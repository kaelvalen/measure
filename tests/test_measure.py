"""Unit tests for measure.py (stdlib unittest). Run: python3 -m unittest discover -s tests"""
import contextlib
import html.parser
import io
import json
import os
import random
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.join(HERE, "..", "skills", "measure", "scripts"))

import measure  # noqa: E402


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = measure.main(argv)
    return code, out.getvalue(), err.getvalue()


class CollapserTests(unittest.TestCase):
    def test_perf_script(self):
        stacks = measure.collapse_perf(fixture("perf.script.txt"))
        self.assertEqual(measure.format_folded(stacks), fixture("perf.expected.folded"))

    def test_cpuprofile(self):
        stacks = measure.collapse_cpuprofile(json.loads(fixture("node.cpuprofile")))
        self.assertEqual(measure.format_folded(stacks), fixture("node.expected.folded"))

    def test_pprof_traces(self):
        stacks = measure.collapse_pprof_traces(fixture("go.traces.txt"))
        self.assertEqual(measure.format_folded(stacks), fixture("go.expected.folded"))

    def test_folded_passthrough(self):
        self.assertEqual(measure.parse_folded(fixture("hotspots.folded")), {
            "main;parse;tokenize": 30, "main;parse": 10, "main;render": 4, "main;log": 1,
            "main;fib;fib;fib": 40, "main;fib": 15})

    def test_sniff(self):
        cases = {"perf.script.txt": ("perf-script", "perf.expected.folded"),
                 "node.cpuprofile": ("cpuprofile", "node.expected.folded"),
                 "go.traces.txt": ("pprof-traces", "go.expected.folded"),
                 "perf.expected.folded": ("folded", "perf.expected.folded")}
        for name, (fmt, expected) in cases.items():
            stacks, got = measure.sniff_and_collapse(os.path.join(FIXTURES, name))
            self.assertEqual(got, fmt, name)
            self.assertEqual(measure.format_folded(stacks), fixture(expected), name)

    def test_pyspy_normalization(self):
        raw = {'process 101:"";process 102:"python3 app.py";main (app.py:1);f (app.py:5)': 7,
               'process 101:"";process 103:"python3 app.py";main (app.py:1);f (app.py:5)': 3}
        self.assertEqual(measure.normalize_pyspy(raw),
                         {'process "python3 app.py";main (app.py:1);f (app.py:5)': 10})


class HotspotTests(unittest.TestCase):
    def setUp(self):
        stacks = measure.parse_folded(fixture("hotspots.folded"))
        self.total, rows = measure.compute_hotspots(stacks, 0.02)
        self.rows = {r["frame"]: r for r in rows}

    def test_total_and_roots(self):
        self.assertEqual(self.total, 100)
        self.assertNotIn("main", self.rows)  # 100% inclusive: a root

    def test_self_and_incl(self):
        self.assertEqual((self.rows["parse"]["self"], self.rows["parse"]["incl"]), (10, 40))
        self.assertEqual((self.rows["tokenize"]["self"], self.rows["tokenize"]["incl"]), (30, 30))

    def test_recursion_counted_once(self):
        fib = self.rows["fib"]
        self.assertEqual(fib["self"], 55)
        self.assertEqual(fib["incl"], 55)  # not 3 * 40 + 15

    def test_amdahl_gate(self):
        self.assertAlmostEqual(self.rows["fib"]["max_gain"], 1 / 0.45)
        self.assertFalse(self.rows["render"]["gated"])  # 4% -> 1.042x
        self.assertTrue(self.rows["log"]["gated"])      # 1% -> 1.010x <= 1.02x

    def test_printed_table_marks_gate(self):
        code, out, _ = run_main(["hotspots", os.path.join(FIXTURES, "hotspots.folded")])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertIn("✗", [l for l in lines if l.endswith("   log")][0])
        self.assertNotIn("✗", [l for l in lines if l.endswith("   fib")][0])


def noisy(rng, center, spread, n):
    return [center * (1 + rng.uniform(-spread, spread)) for _ in range(n)]


class StatisticsTests(unittest.TestCase):
    def verdict(self, a, b, higher_better=False, seed=11, m=0.02):
        lo, hi = measure.bootstrap_ci(a, b, higher_better, seed)
        return measure.decide(lo, hi, m), lo, hi

    def test_improved(self):
        rng = random.Random(1)
        v, lo, _ = self.verdict(noisy(rng, 1.0, 0.01, 15), noisy(rng, 0.6, 0.01, 15))
        self.assertEqual(v, measure.IMPROVED)
        self.assertGreater(lo, 1.5)

    def test_regressed(self):
        rng = random.Random(2)
        v, _, _ = self.verdict(noisy(rng, 1.0, 0.01, 15), noisy(rng, 1.3, 0.01, 15))
        self.assertEqual(v, measure.REGRESSED)

    def test_no_effect(self):
        rng = random.Random(3)
        v, lo, hi = self.verdict(noisy(rng, 1.0, 0.004, 30), noisy(rng, 1.0, 0.004, 30))
        self.assertEqual(v, measure.NO_EFFECT)
        self.assertTrue(1 / 1.02 <= lo <= hi <= 1.02)

    def test_inconclusive(self):
        rng = random.Random(4)
        v, _, _ = self.verdict(noisy(rng, 1.0, 0.3, 10), noisy(rng, 1.0, 0.3, 10))
        self.assertEqual(v, measure.INCONCLUSIVE)

    def test_higher_better_flips_direction(self):
        rng = random.Random(5)
        a, b = noisy(rng, 100.0, 0.01, 15), noisy(rng, 150.0, 0.01, 15)
        self.assertEqual(self.verdict(a, b, higher_better=True)[0], measure.IMPROVED)
        self.assertEqual(self.verdict(a, b, higher_better=False)[0], measure.REGRESSED)

    def test_bootstrap_deterministic(self):
        rng = random.Random(6)
        a, b = noisy(rng, 1.0, 0.05, 15), noisy(rng, 0.9, 0.05, 15)
        self.assertEqual(measure.bootstrap_ci(a, b, False, 42), measure.bootstrap_ci(a, b, False, 42))

    def test_decide_boundaries(self):
        self.assertEqual(measure.decide(1.03, 1.10, 0.02), measure.IMPROVED)
        self.assertEqual(measure.decide(1.00, 1.03, 0.02), measure.INCONCLUSIVE)
        self.assertEqual(measure.decide(0.99, 1.01, 0.02), measure.NO_EFFECT)
        self.assertEqual(measure.decide(0.90, 0.97, 0.02), measure.REGRESSED)

    def test_summary(self):
        s = measure.summarize([1.0, 2.0, 3.0, 4.0, 100.0])
        self.assertEqual(s["median"], 3.0)
        self.assertEqual(s["mad"], 1.0)
        self.assertEqual((s["min"], s["max"], s["n"]), (1.0, 100.0, 5))


class ScheduleTests(unittest.TestCase):
    def test_balanced(self):
        sched = measure.make_schedule(25, 7)
        flat = "".join(sched)
        self.assertEqual(flat.count("A"), flat.count("B"))
        self.assertTrue(all(p in ("AB", "BA") for p in sched))
        self.assertIn("AB", sched)
        self.assertIn("BA", sched)

    def test_reproducible_and_prefix_stable(self):
        self.assertEqual(measure.make_schedule(30, 99), measure.make_schedule(30, 99))
        self.assertEqual(measure.make_schedule(40, 99)[:30], measure.make_schedule(30, 99))
        self.assertNotEqual(measure.make_schedule(30, 99), measure.make_schedule(30, 100))


class OutputEquivalenceTests(unittest.TestCase):
    def test_normalization_strips_parse_and_ignored_lines(self):
        patterns = [re.compile(r"tok/s: ([\d.]+)"), re.compile(r"^elapsed")]
        a = measure.normalize_output("result 42\ntok/s: 101.5\nelapsed 3.2s\n", patterns)
        b = measure.normalize_output("result 42\ntok/s: 180.0\nelapsed 1.9s\n", patterns)
        self.assertEqual(a, ["result 42"])
        self.assertEqual(measure.output_hash(a), measure.output_hash(b))

    def test_first_difference(self):
        self.assertIsNone(measure.first_difference(["x", "y"], ["x", "y"]))
        self.assertEqual(measure.first_difference(["x", "y"], ["x", "z"]), {"line": 2, "a": "y", "b": "z"})
        self.assertEqual(measure.first_difference(["x"], ["x", "y"]), {"line": 2, "a": "<EOF>", "b": "y"})


class _SvgCollector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.svgs = []
        self.root_widths = []
        self._in_root = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "svg":
            self.svgs.append(a)
        elif tag == "g":
            self._in_root = "root" in (a.get("class") or "").split()
        elif tag == "rect" and self._in_root:
            self.root_widths.append(float(a["width"]))
            self._in_root = False


class DiffTests(unittest.TestCase):
    BASE = {"main;normalize;compile": 40, "main;normalize": 20, "main;dedupe": 30, "main;report": 10}
    CAND = {"main;normalize": 18, "main;dedupe": 1, "main;report": 10, "main;fast_path": 3}

    def test_html_parses_two_svgs_after_narrower(self):
        doc, _, _, scaled = measure.render_diff_html(self.BASE, self.CAND, a_time=1.0, b_time=0.4)
        self.assertTrue(scaled)
        p = _SvgCollector()
        p.feed(doc)
        p.close()
        self.assertEqual(len(p.svgs), 2)
        self.assertEqual(p.svgs[0]["id"], "before")
        before_px, after_px = float(p.svgs[0]["data-total-px"]), float(p.svgs[1]["data-total-px"])
        self.assertAlmostEqual(after_px / before_px, 0.4, places=3)
        self.assertEqual(len(p.root_widths), 2)
        self.assertLess(p.root_widths[1], p.root_widths[0])
        # self-contained: no external requests
        self.assertNotIn("http://", doc.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", doc)

    def test_changes_table(self):
        _, changes, _, _ = measure.render_diff_html(self.BASE, self.CAND, a_time=1.0, b_time=0.4)
        self.assertEqual(changes[0]["frame"], "compile")  # 0.4s of self time, gone entirely
        self.assertAlmostEqual(changes[0]["delta"], -0.4)
        new = [c for c in changes if c["frame"] == "fast_path"][0]
        self.assertIsNone(new["delta_pct"])

    def test_normalized_without_times(self):
        doc, _, _, scaled = measure.render_diff_html(self.BASE, self.CAND)
        self.assertFalse(scaled)
        self.assertIn("normalized", doc)

    def test_line_numbers_ignored_across_edits(self):
        base = {"main (app.py:1);normalize (app.py:34)": 50, "main (app.py:1)": 50}
        cand = {"main (app.py:1);normalize (app.py:37)": 20, "main (app.py:1)": 50}
        _, changes, _, _ = measure.render_diff_html(base, cand, a_time=1.0, b_time=0.7)
        norm = [c for c in changes if c["frame"] == "normalize (app.py)"]
        self.assertEqual(len(norm), 1)
        self.assertAlmostEqual(norm[0]["delta"], -0.3)
        self.assertEqual(measure.strip_line_numbers({"f idx.js:10;g": 1}), {"f idx.js;g": 1})

    def test_colors(self):
        self.assertTrue(measure.diff_color(-1.0)[0].startswith("hsl(212"))
        self.assertTrue(measure.diff_color(1.0)[0].startswith("hsl(2,"))
        self.assertTrue(measure.diff_color(0.01)[0].startswith("hsl(38"))
        self.assertEqual(measure.change_ratio(10, 0), -1.0)
        self.assertEqual(measure.change_ratio(0, 10), 1.0)


class CliTests(unittest.TestCase):
    def test_detect_lang(self):
        cases = {"python3 app.py": "python", "python3.12 -m x": "python", "uv run app.py": "python",
                 "pytest -q": "python", "poetry run python x.py": "python", "FOO=1 python x.py": "python",
                 "node index.js": "node", "npx tsx a.ts": "node", "bun run x": "node",
                 "go test -bench . ./pkg": "go", "./target/release/app": "native",
                 "cargo run --release": "native"}
        for cmd, lang in cases.items():
            self.assertEqual(measure.detect_lang(cmd), lang, cmd)

    def test_join_command(self):
        self.assertEqual(measure.join_command(["echo hi | wc -c"]), "echo hi | wc -c")
        self.assertEqual(measure.join_command(["python3", "-c", "print(1)"]), "python3 -c 'print(1)'")

    def test_bench_parse_metric(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "b.json")
            code, text, _ = run_main(["bench", "--runs", "3", "--warmup", "0", "--parse", r"score: ([\d.]+)",
                                      "--higher-better", "-o", out, "--", "echo score: 12.5"])
            self.assertEqual(code, 0, text)
            with open(out) as f:
                self.assertEqual(json.load(f)["samples"], [12.5, 12.5, 12.5])

    def test_workload_failure_exit_4(self):
        code, _, err = run_main(["bench", "--runs", "2", "--warmup", "0", "--", "echo boom >&2; exit 3"])
        self.assertEqual(code, measure.EXIT_WORKLOAD)
        self.assertIn("boom", err)

    def test_strict_output_exit_5(self):
        code, out, _ = run_main(["compare", "--runs", "2", "--warmup", "0", "--seed", "1", "--strict-output",
                                 "--a", "echo one", "--b", "echo two"])
        self.assertEqual(code, measure.EXIT_OUTPUT_DIFF)
        self.assertIn("WARNING: outputs differ", out)

    def test_compare_json_records_everything(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "c.json")
            code, text, _ = run_main(["compare", "--runs", "4", "--warmup", "0", "--seed", "5",
                                      "--parse", r"v=(\d+)", "--ignore-line", "^noise", "-o", out,
                                      "--a", "echo v=10; echo noise $$", "--b", "echo v=10; echo noise $$"])
            with open(out) as f:
                data = json.load(f)
        self.assertEqual(code, measure.EXIT_NO_EFFECT, text)  # identical parsed metric
        self.assertEqual(data["verdict"], measure.NO_EFFECT)
        self.assertTrue(data["verdict_line"].startswith("VERDICT NO_EFFECT"))
        self.assertEqual(data["seed"], 5)
        self.assertEqual(data["schedule"], measure.make_schedule(4, 5))
        self.assertEqual([s["side"] for s in data["samples"]], list("".join(measure.make_schedule(4, 5))))
        self.assertTrue(data["outputs_identical"])

    def test_rss_metric(self):
        code, out, _ = run_main(["compare", "--metric", "rss", "--runs", "3", "--warmup", "0", "--seed", "1",
                                 "--a", "python3 -c 'x = bytearray(80_000_000)'",
                                 "--b", "python3 -c 'x = bytearray(8_000_000)'"])
        self.assertEqual(code, measure.EXIT_OK, out)
        self.assertIn("MiB →", out)

    def test_timeout_is_workload_failure(self):
        code, _, err = run_main(["bench", "--runs", "1", "--warmup", "0", "--timeout", "0.3", "--", "sleep 5"])
        self.assertEqual(code, measure.EXIT_WORKLOAD)
        self.assertIn("timed out", err)


if __name__ == "__main__":
    unittest.main()
