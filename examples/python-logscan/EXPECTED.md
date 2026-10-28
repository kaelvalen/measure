# logscan demo: expected agent behavior

`logscan.py` summarizes about 120k synthetic log lines (`gen.py`, fixed seed) in roughly 0.5 to 1 s. It contains two real problems, a decoy and a trap. A Claude Code session with the measure skill should go through the five steps below. The verdict lines quoted here come from a real run on a loaded laptop (`load 7.8(!)`, `turbo=on(!)`); your numbers will differ, but the verdicts should not.

## Setup for a recording

Run the demo from a fresh copy, so the agent cannot read this file or the reference fix:

```
mkdir -p /tmp/logscan && cp gen.py logscan.py /tmp/logscan && cd /tmp/logscan
git init -q && git add -A && git commit -qm init
mkdir -p .measure/workloads && python3 gen.py -o .measure/workloads/data.log
claude "logscan.py is slow on .measure/workloads/data.log. Make it faster."
```

## 1. Profile, and decline the decoy

The first profile of the baseline (abridged):

```
  self%   incl%   max gain   frame
   41.8    52.7      2.12x      normalize (logscan.py:34)
   29.1    29.1      1.41x      dedupe (logscan.py:41)
   13.3    98.4     62.20x      scan (logscan.py:107)
    6.9    10.1      1.11x      _compile (re/__init__.py:330)
    2.4     2.9      1.03x      parse_ts (logscan.py:24)
    0.2     0.2      1.00x  ✗   render_report (logscan.py:50)
```

`render_report` is the decoy: hand-rolled top-15 selection with nested `while` loops and character-by-character padding. It looks like the worst code in the file, and it runs once. The agent must say it is behind the Amdahl gate (✗, max gain 1.00x) and leave it alone.

## 2. Fix `normalize`: IMPROVED, kept

Hypothesis: `Target normalize (41.8% self / 52.7% incl, max gain 2.12x): re.compile with re.IGNORECASE on every call; the cache lookup and flag handling (_compile, enum __get__) cost ~13%.`
Fix: compile the masks once at module level (see `fixed/logscan.py`).

```
VERDICT IMPROVED  gain 1.19x [1.15, 1.24] 95% CI  median 480.0ms → 402.0ms  n=15+15 interleaved  cv 3.4%/6.0%
```

## 3. Fix `dedupe`: IMPROVED, kept

Hypothesis: `Target dedupe (29.8% self / 29.8% incl, max gain 1.42x): "t not in seen" scans a list of 320 templates for each of 120k lines.`
Fix: `list(dict.fromkeys(templates))`.

```
VERDICT IMPROVED  gain 1.33x [1.28, 1.44] 95% CI  median 390.9ms → 292.9ms  n=15+15 interleaved  cv 15.8%/5.2%
```

## 4. Try `parse_ts`: NO_EFFECT, reverted

After the two fixes, the total has shrunk and `parse_ts` crosses the gate (about 3.5 to 6% inclusive, max gain about 1.04x to 1.06x). Its docstring even says "strptime is slow". The tempting change is to replace `datetime.strptime` with `datetime.fromisoformat` or manual slicing.

It cannot help. `parse_ts` already caches by timestamp, the log has only 30 distinct seconds, so strptime runs 30 times. Its one-time initialization is still paid by the report's `date:` line. The frame's share is per-call overhead, not strptime: in the table, `_strptime` itself is ✗.

```
VERDICT INCONCLUSIVE  gain 0.992x [0.955, 1.024] 95% CI  median 289.4ms → 291.8ms  n=60+60 interleaved  cv 9.3%/16.6%  (run budget exhausted: treat as NO_EFFECT)
```

On a quiet machine this is `NO_EFFECT`; on the loaded recording machine the budget ran out first. Either way the rule is the same: **revert**, and list `parse_ts` under rejected hypotheses.

## 5. Final report

The cumulative line comes from its own compare of the original baseline against the final tree, not from multiplying 1.19 × 1.33:

```
## Performance report
Workload: python3 logscan.py .measure/workloads/data.log   Env: linux 7.3 · 16c · governor=performance · turbo=on(!) · load 7.8(!)   Runs: 15+ per side, interleaved
| # | Change | Target frame (share) | Verdict | Gain (95% CI) | Kept |
|---|--------|----------------------|---------|---------------|------|
| 1 | precompile normalize masks | normalize (52.7% incl) | IMPROVED | 1.19x [1.15, 1.24] | yes |
| 2 | dict-based dedupe | dedupe (29.8% incl) | IMPROVED | 1.33x [1.28, 1.44] | yes |
| 3 | fromisoformat in parse_ts | parse_ts (3.7% incl) | INCONCLUSIVE → NO_EFFECT | 0.992x [0.955, 1.024] | no, reverted |
Cumulative: VERDICT IMPROVED  gain 1.62x [1.53, 1.73] 95% CI  median 463.3ms → 286.5ms  n=15+15 interleaved  cv 7.9%/5.9%
Rejected hypotheses: render_report — Amdahl gate (0.2%, max gain 1.00x); parse_ts — NO_EFFECT (budget exhausted)
Diff flamegraph: .measure/diff-final.html
```

## Pass criteria

A session reproduces the demo when it:
1. profiles before editing, and explicitly declines `render_report` because of the gate;
2. fixes `normalize` and gets IMPROVED;
3. fixes `dedupe` and gets IMPROVED (steps 2 and 3 may come in either order, as separate compares);
4. tries a strptime replacement in `parse_ts`, gets NO_EFFECT or budget-exhausted INCONCLUSIVE, and reverts it;
5. ends with the report, a cumulative compare and a diff flame graph.

An agent that skips step 4 because it reads `_strptime` as ✗, or that cuts `parse_ts`'s per-call overhead and *measures* a real gain, is behaving correctly too, but it is not the scripted demo. Record another take.

## Tuning knobs

If the shares drift on other hardware or Python versions: `gen.py -n` (line count: total runtime), the 20 × 16 subject/action lists (320 templates: `dedupe` share), the three `MASKS` (`normalize` share) and `--seconds` (distinct timestamps: how much a strptime rewrite could save; keep it small).
