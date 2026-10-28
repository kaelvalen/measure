"""A/A smoke test: the demo workload against an identical copy in a different directory.

This mirrors what the skill's protocol compares (.measure/base vs the working tree): the
same code at paths of different lengths, which shifts the environment size and memory
layout. Randomized order cannot cancel a bias tied to the side, so this is the case the
exact false-positive bound (tests/test_sequential_exact.py) does not cover. It prints the
verdicts and exits 1 if compare fails (exit 1, 4 or 5) or if any A/A compare reads
IMPROVED or REGRESSED, which would mean the directory alone moves the timing past the
2% minimum effect.

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
        side_a = os.path.join(work, "a")
        side_b = os.path.join(work, "a-much-longer-directory-name-for-the-b-side", "b")
        for d in (side_a, side_b):
            os.makedirs(d)
            shutil.copy(os.path.join(EXAMPLE, "fixed", "logscan.py"), d)
        data = os.path.join(work, "data.log")
        subprocess.check_call([sys.executable, os.path.join(EXAMPLE, "gen.py"), "-o", data])
        cmd = "python3 logscan.py %s" % data
        counts = {}
        for i in range(args.reps):
            out = os.path.join(work, "self-%d.json" % i)
            proc = subprocess.run([sys.executable, MEASURE, "compare", "--a-cwd", side_a, "--b-cwd", side_b,
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
    biased = sum(n for v, n in counts.items() if v.startswith(("IMPROVED", "REGRESSED")))
    if biased:
        print("FAIL: %d A/A compare(s) found a difference between identical code" % biased)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
