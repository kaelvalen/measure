#!/usr/bin/env bash
# End-to-end check against examples/python-logscan (spec 9.2). Needs py-spy on PATH.
# Usage: tests/e2e.sh
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
EX="$ROOT/examples/python-logscan"
M="python3 $ROOT/skills/measure/scripts/measure.py"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

fail() { echo "E2E FAIL: $*" >&2; exit 1; }
verdict() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["verdict"])' "$1"; }

mkdir -p "$WORK/base" "$WORK/cand"
cp "$EX/logscan.py" "$WORK/base/"
cp "$EX/logscan.py" "$WORK/cand/"
python3 "$EX/gen.py" -o "$WORK/data.log"
CMD="python3 logscan.py $WORK/data.log"

echo "== 1. doctor"
$M doctor

echo "== 2. profile (py-spy)"
$M profile --cwd "$WORK/base" -o "$WORK/prof-base" -- "$CMD" | tee "$WORK/profile.txt"
grep -q "normalize (logscan.py" "$WORK/profile.txt" || fail "normalize missing from hotspots"
# On a fast machine the decoy may get no samples at all, which also keeps it out of reach.
if grep -q "render_report" "$WORK/profile.txt"; then
  grep -Eq "✗ +render_report" "$WORK/profile.txt" || fail "render_report (decoy) not marked by the Amdahl gate"
fi

echo "== 3. apply the reference fix"
cp "$EX/fixed/logscan.py" "$WORK/cand/logscan.py"

echo "== 4. compare base vs fixed: expect IMPROVED, exit 0"
set +e
$M compare --a-cwd "$WORK/base" --b-cwd "$WORK/cand" --until-conclusive -o "$WORK/cmp.json" -- "$CMD"
rc=$?
set -e
[ "$rc" -eq 0 ] || fail "compare exit $rc, expected 0"
[ "$(verdict "$WORK/cmp.json")" = IMPROVED ] || fail "verdict $(verdict "$WORK/cmp.json"), expected IMPROVED"

echo "== 5. A/A: base vs an identical copy at a longer path: expect NO_EFFECT, exit 2"
mkdir -p "$WORK/a-much-longer-directory-name-for-the-copy/aa"
cp "$EX/logscan.py" "$WORK/a-much-longer-directory-name-for-the-copy/aa/"
set +e
$M compare --a-cwd "$WORK/base" --b-cwd "$WORK/a-much-longer-directory-name-for-the-copy/aa" --until-conclusive -o "$WORK/self.json" -- "$CMD"
rc=$?
set -e
[ "$rc" -eq 2 ] || fail "A/A compare exit $rc, expected 2"
[ "$(verdict "$WORK/self.json")" = NO_EFFECT ] || fail "A/A verdict $(verdict "$WORK/self.json"), expected NO_EFFECT"

echo "== 6. diff"
$M profile --cwd "$WORK/cand" -o "$WORK/prof-cand" -- "$CMD" > /dev/null
$M diff "$WORK/prof-base.folded" "$WORK/prof-cand.folded" --compare "$WORK/cmp.json" -o "$WORK/diff.html"
[ -s "$WORK/diff.html" ] || fail "diff.html missing"
[ "$(grep -o '<svg ' "$WORK/diff.html" | wc -l)" -eq 2 ] || fail "diff.html should contain two flame graphs"

echo "E2E OK"
