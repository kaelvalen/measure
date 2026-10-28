"""Self-compare false-positive rate (spec 9.4): compare a workload against itself N times.

A false positive is any IMPROVED or REGRESSED verdict for identical code. Exits 1 if the
rate exceeds --max-rate.

Usage: python3 tests/false_positive_rate.py [--reps 20] [--max-rate 0.05]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEASURE = os.path.join(ROOT, "skills", "measure", "scripts", "measure.py")
EXAMPLE = os.path.join(ROOT, "examples", "python-logscan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--max-rate", type=float, default=0.05)
    args = ap.parse_args()

    work = tempfile.mkdtemp()
    try:
        shutil.copy(os.path.join(EXAMPLE, "fixed", "logscan.py"), work)
        data = os.path.join(work, "data.log")
        subprocess.check_call([sys.executable, os.path.join(EXAMPLE, "gen.py"), "-o", data])
        cmd = "python3 logscan.py %s" % data
        counts = {}
        for i in range(args.reps):
            out = os.path.join(work, "self-%d.json" % i)
            subprocess.run([sys.executable, MEASURE, "compare", "--a-cwd", work, "--b-cwd", work,
                            "--until-conclusive", "--seed", str(1000 + i), "-o", out, "--", cmd],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with open(out) as f:
                result = json.load(f)
            v = result["verdict"] + (" (budget exhausted)" if result["budget_exhausted"] else "")
            counts[v] = counts.get(v, 0) + 1
            print("%2d  %s" % (i + 1, result["verdict_line"]), flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    false_pos = sum(n for v, n in counts.items() if v.startswith(("IMPROVED", "REGRESSED")))
    rate = false_pos / float(args.reps)
    print("verdicts: %s" % ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    print("false-positive rate: %d/%d = %.0f%% (max %.0f%%)" % (false_pos, args.reps, rate * 100,
                                                               args.max_rate * 100))
    return 1 if rate > args.max_rate else 0


if __name__ == "__main__":
    sys.exit(main())
