"""Self-compare smoke test: compare the demo workload against itself a few times.

This shows that compare runs end to end on a real machine and prints what it decided. It
does not establish the false-positive rate: 20 clean repetitions bound that only at about
14%, and the noise on a shared runner changes from day to day. The rate comes from
tests/simulate_null.py. Exits 1 if compare fails (exit 1, 4 or 5), not on its verdict.

Usage: python3 tests/self_compare_smoke.py [--reps 3]
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
    ap.add_argument("--reps", type=int, default=3)
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
            proc = subprocess.run([sys.executable, MEASURE, "compare", "--a-cwd", work, "--b-cwd", work,
                                   "--until-conclusive", "--seed", str(1000 + i), "-o", out, "--", cmd],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            if proc.returncode not in (0, 2, 3):
                print(proc.stdout)
                print("FAIL: compare exited %d" % proc.returncode)
                return 1
            with open(out) as f:
                result = json.load(f)
            v = result["verdict"] + (" (budget exhausted)" if result["budget_exhausted"] else "")
            counts[v] = counts.get(v, 0) + 1
            print("%2d  %s" % (i + 1, result["verdict_line"]), flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("verdicts: %s" % ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
