#!/usr/bin/env python3
"""measure: profile before you optimize, prove it or revert it.

Subcommands:
  doctor     check the machine for benchmarking noise and profiler access
  bench      time one command
  compare    interleaved A/B benchmark with a paired CI and a verdict
  profile    sample a command and write collapsed stacks (PREFIX.folded)
  hotspots   self/inclusive table with the Amdahl gate
  diff       before/after diff flame graph as a self-contained HTML file

Everything after `--` is the workload command, run through `sh -c`.
Single file, Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import html
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time

VERSION = "0.1.0"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_EFFECT = 2
EXIT_REGRESSED = 3
EXIT_WORKLOAD = 4
EXIT_OUTPUT_DIFF = 5

IMPROVED = "IMPROVED"
REGRESSED = "REGRESSED"
NO_EFFECT = "NO_EFFECT"
INCONCLUSIVE = "INCONCLUSIVE"
VERDICT_EXIT = {IMPROVED: EXIT_OK, NO_EFFECT: EXIT_NO_EFFECT,
                INCONCLUSIVE: EXIT_NO_EFFECT, REGRESSED: EXIT_REGRESSED}

CI_LEVEL = 0.95
BATCH_PAIRS = 10
CV_WARN = 0.05
ROOT_SHARE = 0.995

# Fixes printed when a profiler is missing or blocked. Keep in sync with
# reference/profilers.md.
FIXES = {
    "py-spy-missing": (
        "py-spy not found. Install it:\n"
        "  pip install py-spy        (or: pipx install py-spy / uv tool install py-spy)\n"
        "See reference/profilers.md, section py-spy."),
    "py-spy-perm": (
        "py-spy could not read the target process (ptrace denied).\n"
        "  Docker:  run the container with --cap-add SYS_PTRACE\n"
        "  macOS:   run measure with sudo\n"
        "  Linux:   sudo sysctl -w kernel.yama.ptrace_scope=1\n"
        "See reference/profilers.md, section py-spy."),
    "perf-missing": (
        "perf not found. Install it:\n"
        "  Debian/Ubuntu: sudo apt install linux-tools-common linux-tools-$(uname -r)\n"
        "  Fedora:        sudo dnf install perf\n"
        "  Arch:          sudo pacman -S perf\n"
        "See reference/profilers.md, section perf."),
    "perf-perm": (
        "perf is blocked by kernel.perf_event_paranoid.\n"
        "  sudo sysctl -w kernel.perf_event_paranoid=1\n"
        "  persist: echo 'kernel.perf_event_paranoid=1' | sudo tee /etc/sysctl.d/99-perf.conf\n"
        "See reference/profilers.md, section perf."),
    "perf-macos": (
        "Native profiling is not supported on macOS in measure v1 (no perf).\n"
        "Python (py-spy) and Node (--cpu-prof) profiling still work. For native code,\n"
        "export collapsed stacks from another tool and use: measure profile --from FILE"),
    "node-missing": "node not found on PATH. Install Node.js 12+ (needed for --cpu-prof).",
    "go-missing": "go not found on PATH. Install Go: https://go.dev/doc/install",
}


class MeasureError(Exception):
    def __init__(self, message, code=EXIT_ERROR):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Formatting and statistics
# ---------------------------------------------------------------------------

def fmt_time(s):
    if s is None:
        return "-"
    a = abs(s)
    if a >= 100:
        return "%.1fs" % s
    if a >= 1:
        return "%.3fs" % s
    if a >= 1e-3:
        return "%.1fms" % (s * 1e3)
    return "%.1fµs" % (s * 1e6)


METRIC_JSON = {"time": "wall_time_s", "rss": "peak_rss_mib", "parsed": "parsed"}


def metric_kind(args):
    """'time' (wall clock), 'rss' (peak RSS in MiB) or 'parsed' (--parse)."""
    if args.parse and args.metric == "rss":
        raise MeasureError("--parse and --metric rss are mutually exclusive")
    return "parsed" if args.parse else args.metric


def metric_label(kind, parse):
    return {"time": "wall time", "rss": "peak RSS"}.get(kind) or "parsed %r" % parse


def sample_value(res, kind, parse_re):
    if kind == "time":
        return res.wall
    if kind == "rss":
        return res.rss_mib
    return extract_metric(parse_re, res.stdout, res.stderr)


def fmt_value(v, kind):
    if kind == "time":
        return fmt_time(v)
    if kind == "rss":
        return "%.1fMiB" % v
    return "%.4g" % v


def fmt_ratios(*values):
    """Format gain ratios with 3 decimals when all sit within 10% of 1."""
    finite = [v for v in values if math.isfinite(v)]
    digits = 3 if finite and max(abs(v - 1) for v in finite) < 0.1 else 2
    return ["%.*f" % (digits, v) if math.isfinite(v) else "inf" for v in values]


def summarize(xs):
    n = len(xs)
    med = statistics.median(xs)
    mean = statistics.fmean(xs)
    sd = statistics.stdev(xs) if n > 1 else 0.0
    return {
        "n": n, "median": med, "mean": mean, "sd": sd,
        "mad": statistics.median([abs(x - med) for x in xs]),
        "min": min(xs), "max": max(xs),
        "cv": sd / abs(mean) if mean else 0.0,
    }


def pair_log_gains(a, b, higher_better):
    """log gain of each pair (a[i], b[i]); above 0 means B was better in that pair."""
    if len(a) != len(b):
        raise MeasureError("paired analysis needs one A and one B value per pair")
    if min(a) <= 0 or min(b) <= 0:
        raise MeasureError("the gain is a ratio, so metric values must be positive")
    if higher_better:
        return [math.log(y / x) for x, y in zip(a, b)]
    return [math.log(x / y) for x, y in zip(a, b)]


def paired_gain(a, b, higher_better):
    """Point estimate: exp(median of per-pair log gains)."""
    return math.exp(statistics.median(pair_log_gains(a, b, higher_better)))


def _binom_half_cdf(k, n):
    """P(X <= k) for X ~ Binomial(n, 1/2)."""
    return sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n  # int / int: no overflow for large n


def median_ci_rank(n, level):
    """Largest k such that [d_(k), d_(n+1-k)] covers the median with probability >= level.

    0 when n is too small for any interval at that level.
    """
    k = 0
    while k < n // 2 and 2 * _binom_half_cdf(k, n) <= 1.0 - level:
        k += 1
    return k


def min_pairs(level):
    """Fewest pairs that allow an interval at this level."""
    n = 2
    while median_ci_rank(n, level) == 0:
        n += 1
    return n


def gain_ci(a, b, higher_better, level=CI_LEVEL):
    """Distribution-free CI of the paired gain (sign-test inversion on per-pair log gains).

    Pair i is a[i] and b[i], run back to back in a coin-flip order. The per-pair ratio
    cancels what both runs shared (drift, the load at that moment). The coin flip makes
    each pair's noise term equally likely to favor either side, independently of every
    other pair, whatever the noise process is (bursts, waves, outliers). So the interval
    between two order statistics covers the median log gain with exactly binomial
    probability, without assuming independent runs. Returns (0, inf) when there are too
    few pairs for the level.
    """
    d = sorted(pair_log_gains(a, b, higher_better))
    k = median_ci_rank(len(d), level)
    if k == 0:
        return 0.0, math.inf
    return math.exp(d[k - 1]), math.exp(d[len(d) - k])


def look_schedule(runs, max_runs, until_conclusive):
    """Pair counts at which compare computes a verdict."""
    looks = [runs]
    if until_conclusive:
        while looks[-1] < max_runs:
            looks.append(min(looks[-1] + BATCH_PAIRS, max_runs))
    return looks


def look_level(i, n_looks):
    """CI level at look i of n_looks.

    An early stop on an interim look must clear a Bonferroni share of alpha; the last
    look, which nothing follows, uses the nominal level.
    """
    if i == n_looks - 1:
        return CI_LEVEL
    return 1.0 - (1.0 - CI_LEVEL) / n_looks


def evaluate_look(a, b, higher_better, min_effect, i, n_looks):
    """One look of compare: (verdict, lo, hi, level)."""
    level = look_level(i, n_looks)
    lo, hi = gain_ci(a, b, higher_better, level)
    return decide(lo, hi, min_effect), lo, hi, level


def decide(lo, hi, min_effect):
    up = 1.0 + min_effect
    down = 1.0 / up
    if lo > up:
        return IMPROVED
    if hi < down:
        return REGRESSED
    if lo >= down and hi <= up:
        return NO_EFFECT
    return INCONCLUSIVE


def make_schedule(pairs, seed):
    """Randomized block design: each pair runs A and B in a coin-flip order.

    Prefix-stable: make_schedule(n, s) is a prefix of make_schedule(n + k, s).
    """
    rng = random.Random(seed)
    return ["AB" if rng.random() < 0.5 else "BA" for _ in range(pairs)]


# ---------------------------------------------------------------------------
# Running workloads
# ---------------------------------------------------------------------------

class RunResult(object):
    __slots__ = ("wall", "rss_mib", "returncode", "stdout", "stderr", "timed_out")

    def __init__(self, wall, rss_mib, returncode, stdout, stderr, timed_out):
        self.wall = wall
        self.rss_mib = rss_mib
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


def _kill_group(pid, flag=None):
    if flag is not None:
        flag.set()
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def _maxrss_mib(ru):
    if sys.platform == "darwin":
        return ru.ru_maxrss / (1024.0 * 1024.0)
    return ru.ru_maxrss / 1024.0


def run_workload(cmd, cwd=None, timeout=None, env=None):
    """Run `sh -c cmd` once; wall time, peak RSS (MiB), exit code and output."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        timed_out = threading.Event()
        timer = None
        t0 = time.perf_counter()
        proc = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                start_new_session=True)
        if timeout:
            timer = threading.Timer(timeout, _kill_group, (proc.pid, timed_out))
            timer.daemon = True
            timer.start()
        try:
            _, status, ru = os.wait4(proc.pid, 0)
        except BaseException:
            _kill_group(proc.pid)
            proc.wait()
            raise
        finally:
            if timer is not None:
                timer.cancel()
        wall = time.perf_counter() - t0
        proc.returncode = os.waitstatus_to_exitcode(status)
        out.seek(0)
        err.seek(0)
        return RunResult(wall, _maxrss_mib(ru), proc.returncode,
                         out.read().decode("utf-8", "replace"),
                         err.read().decode("utf-8", "replace"),
                         timed_out.is_set())


def tail_lines(text, n=20):
    lines = text.rstrip("\n").splitlines()
    return "\n".join(lines[-n:])


def check_run(res, label, timeout=None):
    if res.timed_out:
        raise MeasureError("%s: workload timed out after %ss and was killed" % (label, timeout),
                           EXIT_WORKLOAD)
    if res.returncode != 0:
        raise MeasureError("%s: workload exited with code %d\n--- last 20 lines of stderr ---\n%s"
                           % (label, res.returncode, tail_lines(res.stderr)), EXIT_WORKLOAD)


def extract_metric(regex, stdout, stderr):
    for text in (stdout, stderr):
        m = regex.search(text)
        if m:
            try:
                return float(m.group(1))
            except (IndexError, ValueError):
                raise MeasureError("--parse matched %r but group 1 is not a number" % m.group(0))
    raise MeasureError("--parse %r matched neither stdout nor stderr of the workload" % regex.pattern)


def compile_regex(pattern, flag):
    try:
        return re.compile(pattern)
    except re.error as e:
        raise MeasureError("invalid %s regex %r: %s" % (flag, pattern, e))


# ---------------------------------------------------------------------------
# Environment (doctor)
# ---------------------------------------------------------------------------

def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def cpu_model():
    if sys.platform.startswith("linux"):
        text = _read("/proc/cpuinfo") or ""
        for key in ("model name", "Model", "Hardware", "cpu model"):
            m = re.search(r"^%s\s*:\s*(.+)$" % re.escape(key), text, re.M)
            if m:
                return m.group(1).strip()
    if sys.platform == "darwin":
        try:
            return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    return platform.processor() or "unknown"


def cpu_governor():
    return _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")


def turbo_state():
    """'on', 'off' or None when the kernel does not expose it."""
    v = _read("/sys/devices/system/cpu/intel_pstate/no_turbo")
    if v in ("0", "1"):
        return "off" if v == "1" else "on"
    for path in ("/sys/devices/system/cpu/cpufreq/boost",
                 "/sys/devices/system/cpu/cpufreq/policy0/boost"):
        v = _read(path)
        if v in ("0", "1"):
            return "on" if v == "1" else "off"
    return None


def on_battery():
    """True on battery, False on mains, None when there is no battery."""
    supplies = glob.glob("/sys/class/power_supply/*")
    batteries = [s for s in supplies if _read(os.path.join(s, "type")) == "Battery"]
    if not batteries:
        return None
    for s in supplies:
        if _read(os.path.join(s, "type")) == "Mains" and _read(os.path.join(s, "online")) == "1":
            return False
    return any(_read(os.path.join(b, "status")) == "Discharging" for b in batteries)


def in_container():
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    cg = _read("/proc/1/cgroup") or ""
    return bool(re.search(r"docker|kubepods|containerd|lxc|podman", cg))


def has_cap_sys_ptrace():
    m = re.search(r"^CapEff:\s*([0-9a-fA-F]+)$", _read("/proc/self/status") or "", re.M)
    return bool(m) and bool(int(m.group(1), 16) & (1 << 19))


def env_line():
    """One-line environment summary for reports."""
    parts = []
    rel = ".".join(platform.release().split("-")[0].split(".")[:2])
    parts.append("%s %s" % (platform.system().lower(), rel))
    parts.append("%dc" % (os.cpu_count() or 0))
    gov = cpu_governor()
    if gov:
        parts.append("governor=%s%s" % (gov, "" if gov == "performance" else "(!)"))
    turbo = turbo_state()
    if turbo:
        parts.append("turbo=%s%s" % (turbo, "(!)" if turbo == "on" else ""))
    if on_battery():
        parts.append("battery(!)")
    try:
        load = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        parts.append("load %.1f%s" % (load, "(!)" if load / cores > 0.3 else ""))
    except OSError:
        pass
    return " · ".join(parts)


LANG_MARKERS = [
    ("pyproject.toml", "python"), ("requirements.txt", "python"), ("setup.py", "python"),
    ("Cargo.toml", "rust"), ("go.mod", "go"), ("package.json", "node"),
    ("CMakeLists.txt", "c/c++"), ("Makefile", "make"),
]

RECOMMENDED = {
    "python": ("py-spy", "measure profile --lang python -- <cmd>   (add --native for C extensions)"),
    "rust": ("perf", "cargo build --release, then measure profile --lang native -- <binary>"),
    "go": ("go", "measure profile --lang go -- go test -run '^$' -bench <Name> <pkg>"),
    "node": ("node", "measure profile --lang node -- node <script>   (uses --cpu-prof)"),
    "c/c++": ("perf", "build with -O2 -fno-omit-frame-pointer, then measure profile --lang native -- <binary>"),
    "make": ("perf", "measure profile --lang native -- <cmd>"),
}


def doctor_checks(cwd="."):
    checks = []

    def add(name, severity, detail, fix=""):
        checks.append({"name": name, "severity": severity, "detail": detail, "fix": fix})

    cores = os.cpu_count() or 1
    add("system", "ok", "%s %s · %s · %d cores · Python %s" % (
        platform.system(), platform.release(), cpu_model(), cores, platform.python_version()))

    gov = cpu_governor()
    if gov is None:
        add("governor", "ok", "not exposed (VM, container or macOS)")
    elif gov == "performance":
        add("governor", "ok", gov)
    else:
        add("governor", "warn", gov, "sudo cpupower frequency-set -g performance")

    turbo = turbo_state()
    if turbo is None:
        add("turbo", "ok", "not exposed")
    elif turbo == "on":
        add("turbo", "warn", "on: clock speed varies with temperature, adding run-to-run variance",
            "echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo   "
            "(AMD/acpi-cpufreq: echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost)")
    else:
        add("turbo", "ok", "off")

    try:
        load = os.getloadavg()[0]
        per = load / cores
        if per > 0.3:
            add("load", "warn", "%.2f over %d cores (%.2f per core)" % (load, cores, per),
                "close busy processes (browsers, builds, indexers) or expect wider CIs")
        else:
            add("load", "ok", "%.2f over %d cores" % (load, cores))
    except OSError:
        add("load", "warn", "unavailable")

    if sys.platform.startswith("linux"):
        par = _read("/proc/sys/kernel/perf_event_paranoid")
        if par is None:
            add("perf_event_paranoid", "warn", "unreadable", "")
        elif int(par) > 1:
            add("perf_event_paranoid", "warn", "%s: perf cannot profile (or only user space)" % par,
                "sudo sysctl -w kernel.perf_event_paranoid=1")
        else:
            add("perf_event_paranoid", "ok", par)

        scope = _read("/proc/sys/kernel/yama/ptrace_scope")
        cap = has_cap_sys_ptrace()
        container = in_container()
        if scope == "3" or (scope == "2" and not cap):
            add("ptrace", "fail", "yama ptrace_scope=%s blocks py-spy" % scope,
                "sudo sysctl -w kernel.yama.ptrace_scope=1")
        elif container and not cap:
            add("ptrace", "warn", "container without CAP_SYS_PTRACE: py-spy will fail",
                "restart the container with --cap-add SYS_PTRACE")
        else:
            add("ptrace", "ok", "permitted (ptrace_scope=%s%s)" % (
                scope if scope is not None else "n/a", ", container" if container else ""))

        bat = on_battery()
        if bat:
            add("power", "warn", "on battery: power management throttles the CPU",
                "plug in the charger")
        else:
            add("power", "ok", "mains" if bat is False else "no battery")
    elif sys.platform == "darwin":
        add("perf", "warn", "macOS: native profiling unsupported in v1 (no perf); Python and Node work",
            "")

    tools = {}
    for t in ("py-spy", "perf", "go", "node", "cargo", "taskset"):
        tools[t] = shutil.which(t)

    langs = []
    for marker, lang in LANG_MARKERS:
        if os.path.exists(os.path.join(cwd, marker)) and lang not in [l for l, _ in langs]:
            langs.append((lang, marker))

    git = {"repo": False, "dirty": None, "head": None}
    try:
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd,
                                capture_output=True, text=True)
        if inside.returncode == 0 and inside.stdout.strip() == "true":
            git["repo"] = True
            st = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                cwd=cwd, capture_output=True, text=True)
            git["dirty"] = bool(st.stdout.strip())
            head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd,
                                  capture_output=True, text=True)
            git["head"] = head.stdout.strip() or None
    except OSError:
        pass
    if not git["repo"]:
        add("git", "warn", "not a git repository: the baseline worktree workflow needs git",
            "git init && git add -A && git commit -m baseline")
    else:
        add("git", "ok", "HEAD %s, tree %s" % (git["head"] or "(no commits)",
                                                 "dirty" if git["dirty"] else "clean"))

    recs = []
    for lang, marker in langs:
        tool, how = RECOMMENDED[lang]
        recs.append({"language": lang, "marker": marker, "profiler": tool,
                     "available": bool(tools.get(tool)), "how": how})
    return {"checks": checks, "tools": tools, "languages": recs, "git": git, "env": env_line()}


def cmd_doctor(args):
    rep = doctor_checks(".")
    if args.json:
        print(json.dumps(rep, indent=2))
        return EXIT_OK
    print("measure doctor")
    width = max(len(c["name"]) for c in rep["checks"])
    for c in rep["checks"]:
        line = "  %-4s  %-*s  %s" % (c["severity"], width, c["name"], c["detail"])
        if c["fix"] and c["severity"] != "ok":
            line += "\n  %-4s  %-*s  fix: %s" % ("", width, "", c["fix"])
        print(line)
    print("  tools   " + "  ".join("%s %s" % (t, "✓" if p else "✗") for t, p in rep["tools"].items()))
    if rep["languages"]:
        print("  languages and recommended profilers:")
        for r in rep["languages"]:
            avail = "" if r["available"] else "   [%s missing]" % r["profiler"]
            print("    %-7s (%s) → %s%s" % (r["language"], r["marker"], r["how"], avail))
    else:
        print("  languages: none detected from marker files in this directory")
    print("env: " + rep["env"])
    return EXIT_OK


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------

def measure_runs(cmd, cwd, n, warmup, kind, parse_re, timeout, label):
    for _ in range(warmup):
        check_run(run_workload(cmd, cwd, timeout), label, timeout)
    values, walls, rss = [], [], []
    for _ in range(n):
        res = run_workload(cmd, cwd, timeout)
        check_run(res, label, timeout)
        walls.append(res.wall)
        rss.append(res.rss_mib)
        values.append(sample_value(res, kind, parse_re))
    return values, walls, rss


def cmd_bench(args):
    if not args.cmd:
        raise MeasureError("bench needs a command after --")
    if args.runs < 1:
        raise MeasureError("--runs must be at least 1")
    kind = metric_kind(args)
    parse_re = compile_regex(args.parse, "--parse") if args.parse else None
    values, walls, rss = measure_runs(args.cmd, args.cwd, args.runs, args.warmup, kind, parse_re,
                                      args.timeout, "bench")
    s = summarize(values)
    rss_med = statistics.median(rss)
    result = {
        "tool": "measure", "version": VERSION, "kind": "bench",
        "cmd": args.cmd, "cwd": os.path.abspath(args.cwd or "."),
        "metric": METRIC_JSON[kind], "parse": args.parse,
        "higher_better": bool(args.higher_better), "runs": args.runs, "warmup": args.warmup,
        "env": env_line(), "summary": s, "rss_mib_median": rss_med,
        "samples": values, "wall_s": walls, "rss_mib": rss,
    }
    if args.output:
        write_json(args.output, result)
    if args.json:
        print(json.dumps(result, indent=2))
        return EXIT_OK
    direction = "higher is better" if args.higher_better else "lower is better"
    print("bench  %s%s" % (args.cmd, "   (cwd %s)" % args.cwd if args.cwd else ""))
    print("runs %d (+%d warmup)   metric: %s (%s)" % (
        args.runs, args.warmup, metric_label(kind, args.parse), direction))
    f = lambda v: fmt_value(v, kind)
    print("  median   %10s" % f(s["median"]))
    print("  mean     %10s" % f(s["mean"]))
    print("  sd       %10s   cv %.1f%%" % (f(s["sd"]), s["cv"] * 100))
    print("  mad      %10s" % f(s["mad"]))
    print("  min      %10s" % f(s["min"]))
    print("  max      %10s" % f(s["max"]))
    print("  peak rss %8.1f MiB (median)" % rss_med)
    if s["cv"] > CV_WARN:
        print("WARNING: cv %.1f%% > 5%%: noisy. Run `measure doctor`, reduce background load, "
              "or lengthen the workload." % (s["cv"] * 100))
    if args.output:
        print("wrote " + args.output)
    return EXIT_OK


def write_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

def normalize_output(text, patterns):
    return [ln for ln in text.splitlines() if not any(p.search(ln) for p in patterns)]


def output_hash(lines):
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def first_difference(a_lines, b_lines):
    for i in range(max(len(a_lines), len(b_lines))):
        a = a_lines[i] if i < len(a_lines) else "<EOF>"
        b = b_lines[i] if i < len(b_lines) else "<EOF>"
        if a != b:
            return {"line": i + 1, "a": a, "b": b}
    return None


def verdict_line(verdict, gain, lo, hi, level, a_sum, b_sum, kind, budget_exhausted):
    g, l, h = fmt_ratios(gain, lo, hi)
    f = lambda v: fmt_value(v, kind)
    line = "VERDICT %s  gain %sx [%s, %s] %g%% CI  median %s → %s  n=%d+%d interleaved  cv %.1f%%/%.1f%%" % (
        verdict, g, l, h, round(level * 100, 1), f(a_sum["median"]), f(b_sum["median"]),
        a_sum["n"], b_sum["n"], a_sum["cv"] * 100, b_sum["cv"] * 100)
    if budget_exhausted:
        line += "  (run budget exhausted: treat as NO_EFFECT)"
    return line


def cmd_compare(args):
    if args.a is not None or args.b is not None:
        if args.a is None or args.b is None:
            raise MeasureError("--a and --b must be given together")
        if args.cmd:
            raise MeasureError("give either --a/--b or a command after --, not both")
        cmd_a, cmd_b = args.a, args.b
    else:
        if not args.cmd or args.a_cwd is None or args.b_cwd is None:
            raise MeasureError("compare needs --a CMD --b CMD, or --a-cwd DIR --b-cwd DIR -- CMD")
        cmd_a = cmd_b = args.cmd
    cwd_a, cwd_b = args.a_cwd, args.b_cwd
    for d in (cwd_a, cwd_b):
        if d is not None and not os.path.isdir(d):
            raise MeasureError("directory not found: %s" % d)
    if args.runs < 2:
        raise MeasureError("--runs must be at least 2")
    if median_ci_rank(args.runs, CI_LEVEL) == 0:
        print("WARNING: %d pairs are too few for a %g%% interval; the verdict will be INCONCLUSIVE "
              "until there are %d." % (args.runs, CI_LEVEL * 100, min_pairs(CI_LEVEL)), file=sys.stderr)
    max_runs = max(args.max_runs, args.runs)
    seed = args.seed if args.seed is not None else int(time.time() * 1000) % (2 ** 31)
    kind = metric_kind(args)
    parse_re = compile_regex(args.parse, "--parse") if args.parse else None
    ignore = [compile_regex(p, "--ignore-line") for p in (args.ignore_line or [])]
    strip = ([parse_re] if parse_re else []) + ignore
    sides = {"A": (cmd_a, cwd_a), "B": (cmd_b, cwd_b)}

    for _ in range(args.warmup):
        for side in "AB":
            check_run(run_workload(sides[side][0], sides[side][1], args.timeout),
                      "warmup %s" % side, args.timeout)

    values = {"A": [], "B": []}
    rss = {"A": [], "B": []}
    outputs = {}
    samples = []
    out_diff = None
    done = 0
    looks = look_schedule(args.runs, max_runs, args.until_conclusive)
    history = []
    for i, target in enumerate(looks):
        schedule = make_schedule(target, seed)
        for pair in range(done, target):
            for side in schedule[pair]:
                cmd, cwd = sides[side]
                res = run_workload(cmd, cwd, args.timeout)
                check_run(res, "side %s (pair %d)" % (side, pair + 1), args.timeout)
                v = sample_value(res, kind, parse_re)
                values[side].append(v)
                rss[side].append(res.rss_mib)
                samples.append({"seq": len(samples), "pair": pair, "side": side, "value": v,
                                "wall_s": res.wall, "rss_mib": res.rss_mib})
                if side not in outputs:
                    outputs[side] = normalize_output(res.stdout, strip)
                    if len(outputs) == 2:
                        out_diff = first_difference(outputs["A"], outputs["B"])
                        if out_diff and args.strict_output:
                            print("WARNING: outputs differ (line %d)\n  A: %s\n  B: %s"
                                  % (out_diff["line"], out_diff["a"], out_diff["b"]))
                            print("--strict-output: aborting before any timing counts.")
                            return EXIT_OUTPUT_DIFF
        done = target
        verdict, lo, hi, level = evaluate_look(values["A"], values["B"], args.higher_better,
                                               args.min_effect, i, len(looks))
        history.append({"pairs": done, "ci_level": level, "ci": [lo, hi], "verdict": verdict})
        print("[measure] %d pairs: %s at %g%% CI" % (done, verdict, round(level * 100, 1)),
              file=sys.stderr)
        if verdict != INCONCLUSIVE:
            break

    budget_exhausted = bool(args.until_conclusive and verdict == INCONCLUSIVE)
    a_sum, b_sum = summarize(values["A"]), summarize(values["B"])
    gain = paired_gain(values["A"], values["B"], args.higher_better)
    line = verdict_line(verdict, gain, lo, hi, level, a_sum, b_sum, kind, budget_exhausted)
    hashes = {s: output_hash(outputs[s]) for s in "AB"}
    env = env_line()
    result = {
        "tool": "measure", "version": VERSION, "kind": "compare",
        "verdict": verdict, "verdict_line": line, "gain": gain, "ci": [lo, hi],
        "ci_level": level,
        "analysis": {"estimator": "exp(median(per-pair log gain))",
                     "ci": "sign-test inversion (order statistics of per-pair log gains)",
                     "ci_ranks": [median_ci_rank(done, level), done + 1 - median_ci_rank(done, level)]},
        "looks": history,
        "min_effect": args.min_effect, "higher_better": bool(args.higher_better),
        "metric": METRIC_JSON[kind], "parse": args.parse,
        "ignore_line": args.ignore_line or [], "seed": seed, "pairs": done,
        "warmup": args.warmup, "until_conclusive": bool(args.until_conclusive),
        "max_runs": max_runs, "budget_exhausted": budget_exhausted, "env": env,
        "a": {"cmd": cmd_a, "cwd": os.path.abspath(cwd_a or "."), "summary": a_sum,
              "rss_mib_median": statistics.median(rss["A"]), "output_sha256": hashes["A"]},
        "b": {"cmd": cmd_b, "cwd": os.path.abspath(cwd_b or "."), "summary": b_sum,
              "rss_mib_median": statistics.median(rss["B"]), "output_sha256": hashes["B"]},
        "outputs_identical": out_diff is None, "output_diff": out_diff,
        "schedule": make_schedule(done, seed), "samples": samples,
    }
    if args.output:
        write_json(args.output, result)
    if args.json:
        print(json.dumps(result, indent=2))
        return VERDICT_EXIT[verdict]

    f = lambda v: fmt_value(v, kind)
    print("compare  A: %s%s" % (cmd_a, "   (cwd %s)" % cwd_a if cwd_a else ""))
    print("         B: %s%s" % (cmd_b, "   (cwd %s)" % cwd_b if cwd_b else ""))
    print("schedule %d pairs, randomized order within each pair, seed %d, +%d warmup each"
          % (done, seed, args.warmup))
    print("metric   %s (%s), min-effect %.1f%%" % (
        metric_label(kind, args.parse),
        "higher is better" if args.higher_better else "lower is better", args.min_effect * 100))
    print("env      " + env)
    print("")
    print("  side  %10s %10s %10s %6s %10s %10s %9s" % ("median", "mean", "sd", "cv", "min", "max", "rss MiB"))
    for side, s in (("A", a_sum), ("B", b_sum)):
        print("  %-4s  %10s %10s %10s %5.1f%% %10s %10s %9.1f" % (
            side, f(s["median"]), f(s["mean"]), f(s["sd"]), s["cv"] * 100, f(s["min"]),
            f(s["max"]), result[side.lower()]["rss_mib_median"]))
    print("")
    if out_diff:
        print("WARNING: outputs differ (line %d)\n  A: %s\n  B: %s" % (out_diff["line"], out_diff["a"], out_diff["b"]))
    else:
        print("outputs  identical (sha256 %s)" % hashes["A"][:12])
    for side, s in (("A", a_sum), ("B", b_sum)):
        if s["cv"] > CV_WARN:
            print("WARNING: cv of %s is %.1f%% > 5%%: noisy environment or workload." % (side, s["cv"] * 100))
    if args.output:
        print("wrote    " + args.output)
    print(line)
    return VERDICT_EXIT[verdict]


# ---------------------------------------------------------------------------
# Collapsed stacks
# ---------------------------------------------------------------------------

_FOLDED_LINE = re.compile(r"^(\S.*?) (\d+(?:\.\d+)?)$")


def parse_folded(text, strict=True):
    stacks = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        m = _FOLDED_LINE.match(line)
        if not m:
            if strict:
                raise MeasureError("not a collapsed stack line (line %d): %r" % (n, line[:120]))
            continue
        c = float(m.group(2))
        stacks[m.group(1)] = stacks.get(m.group(1), 0) + (int(c) if c.is_integer() else c)
    return stacks


def format_folded(stacks):
    out = []
    for stack in sorted(stacks):
        c = stacks[stack]
        if isinstance(c, float) and c.is_integer():
            c = int(c)
        out.append("%s %s" % (stack, c))
    return "\n".join(out) + ("\n" if out else "")


def _add(stacks, frames, count):
    if not frames or count <= 0:
        return
    key = ";".join(frames)
    stacks[key] = stacks.get(key, 0) + count


_PERF_ADDR = re.compile(r"^[0-9a-fA-F]+$")
_PERF_DSO = re.compile(r"\s+\([^()]*\)$")
_PERF_OFFSET = re.compile(r"\+0x[0-9a-fA-F]+$")


def _perf_frame(line):
    s = line.strip()
    parts = s.split(None, 1)
    if parts and _PERF_ADDR.match(parts[0]):
        s = parts[1] if len(parts) > 1 else "[unknown]"
    s = _PERF_DSO.sub("", s)
    s = _PERF_OFFSET.sub("", s)
    return s.strip() or "[unknown]"


def collapse_perf(text):
    """`perf script` output → collapsed stacks. comm is the root frame."""
    stacks = {}
    comm, frames = None, []
    for line in text.splitlines():
        if not line.strip():
            if comm is not None:
                _add(stacks, [comm] + frames[::-1], 1)
            comm, frames = None, []
            continue
        if line.startswith("#"):
            continue
        if not line[0].isspace():
            if comm is not None:
                _add(stacks, [comm] + frames[::-1], 1)
            comm, frames = line.split(None, 1)[0], []
        elif comm is not None:
            frames.append(_perf_frame(line))
    if comm is not None:
        _add(stacks, [comm] + frames[::-1], 1)
    return stacks


def collapse_cpuprofile(data):
    """Chrome/V8 .cpuprofile JSON → collapsed stacks weighted in µs."""
    nodes = {n["id"]: n for n in data["nodes"]}
    parent = {}
    for n in data["nodes"]:
        for c in n.get("children", ()):
            parent[c] = n["id"]

    def label(node):
        cf = node.get("callFrame", {})
        fn = cf.get("functionName") or "(anonymous)"
        url = cf.get("url") or ""
        if not url:
            return fn
        base = url.rstrip("/").rsplit("/", 1)[-1]
        return "%s %s:%d" % (fn, base, int(cf.get("lineNumber", -1)) + 1)

    paths = {}

    def path(nid):
        if nid not in paths:
            frames = []
            cur = nid
            while cur is not None:
                frames.append(label(nodes[cur]))
                cur = parent.get(cur)
            paths[nid] = [f for f in frames[::-1] if f != "(root)"]
        return paths[nid]

    stacks = {}
    samples = data.get("samples")
    if samples is None:
        for nid, n in nodes.items():
            p = path(nid)
            if p and "(idle)" not in p:
                _add(stacks, p, n.get("hitCount", 0))
        return stacks
    deltas = data.get("timeDeltas") or []
    for i, nid in enumerate(samples):
        w = deltas[i] if i < len(deltas) else 0
        p = path(nid)
        if not p or "(idle)" in p:
            continue
        _add(stacks, p, max(int(w), 0))
    return stacks


_PPROF_SEP = re.compile(r"^-+\+-+\s*$")
_PPROF_VALUE = re.compile(r"^\s*(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|min|h|hr|hrs)?\s+(\S.*?)\s*$")
_PPROF_INLINE = re.compile(r" \(inline\)$")
_PPROF_UNIT_US = {None: 1.0, "ns": 1e-3, "us": 1.0, "µs": 1.0, "ms": 1e3, "s": 1e6,
                  "m": 60e6, "min": 60e6, "h": 3600e6, "hr": 3600e6, "hrs": 3600e6}


def collapse_pprof_traces(text):
    """`go tool pprof -traces` output → collapsed stacks weighted in µs."""
    stacks = {}
    blocks, cur, seen_sep = [], [], False
    for line in text.splitlines():
        if _PPROF_SEP.match(line):
            if seen_sep and cur:
                blocks.append(cur)
            cur, seen_sep = [], True
            continue
        if seen_sep and line.strip():
            cur.append(line)
    if seen_sep and cur:
        blocks.append(cur)
    for block in blocks:
        frames, weight = None, 0.0
        for line in block:
            if frames is None:
                m = _PPROF_VALUE.match(line)
                if m:
                    weight = float(m.group(1)) * _PPROF_UNIT_US[m.group(2)]
                    frames = [_PPROF_INLINE.sub("", m.group(3))]
            else:
                frames.append(_PPROF_INLINE.sub("", line.strip()))
        if frames:
            w = round(weight)
            _add(stacks, frames[::-1], int(w) if w >= 1 else weight)
    return stacks


_PYSPY_PROCESS = re.compile(r'^process \d+:(".*")$')


def normalize_pyspy(stacks):
    """Strip PIDs from py-spy process frames and drop the non-Python sh wrapper."""
    out = {}
    for stack, c in stacks.items():
        frames = []
        for f in stack.split(";"):
            m = _PYSPY_PROCESS.match(f)
            if m:
                if m.group(1) == '""':
                    continue
                f = "process " + m.group(1)
            frames.append(f)
        _add(out, frames, c)
    return out


def sniff_and_collapse(path):
    """Detect the format of a profile file and collapse it. Returns (stacks, format)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        raise MeasureError("cannot read %s: %s" % (path, e))
    if path.endswith(".cpuprofile") or text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as e:
            raise MeasureError("%s looks like JSON but does not parse: %s" % (path, e))
        if isinstance(data, dict) and "nodes" in data:
            return collapse_cpuprofile(data), "cpuprofile"
        raise MeasureError("%s: JSON without a 'nodes' array is not a .cpuprofile" % path)
    if not path.endswith(".folded"):
        if any(_PPROF_SEP.match(l) for l in text.splitlines()[:200]):
            return collapse_pprof_traces(text), "pprof-traces"
        if re.search(r"^\S.*\n[ \t]+[0-9a-fA-F]+ \S", text, re.M):
            return collapse_perf(text), "perf-script"
    return parse_folded(text, strict=True), "folded"


# ---------------------------------------------------------------------------
# hotspots
# ---------------------------------------------------------------------------

def compute_hotspots(stacks, min_effect=0.02):
    total = float(sum(stacks.values()))
    self_w, incl_w = {}, {}
    for stack, c in stacks.items():
        frames = stack.split(";")
        leaf = frames[-1]
        self_w[leaf] = self_w.get(leaf, 0) + c
        for f in set(frames):
            incl_w[f] = incl_w.get(f, 0) + c
    rows = []
    if total <= 0:
        return total, rows
    for f, inc in incl_w.items():
        p = inc / total
        if p >= ROOT_SHARE:
            continue
        gain = 1.0 / (1.0 - p)
        rows.append({"frame": f, "self": self_w.get(f, 0), "incl": inc,
                     "self_pct": 100.0 * self_w.get(f, 0) / total, "incl_pct": 100.0 * p,
                     "max_gain": gain, "gated": gain <= 1.0 + min_effect})
    return total, rows


def select_hotspots(rows, top):
    by_self = sorted(rows, key=lambda r: (-r["self"], -r["incl"], r["frame"]))[:top]
    by_incl = sorted(rows, key=lambda r: (-r["incl"], -r["self"], r["frame"]))[:top]
    chosen = {r["frame"]: r for r in by_self + by_incl}
    return sorted(chosen.values(), key=lambda r: (-r["self"], -r["incl"], r["frame"]))


def fmt_gain(g):
    return "%.0fx" % g if g >= 100 else "%.2fx" % g


def _num(x):
    return str(int(x)) if float(x).is_integer() else "%.1f" % x


def print_hotspots(stacks, top, min_effect, source=""):
    total, rows = compute_hotspots(stacks, min_effect)
    chosen = select_hotspots(rows, top)
    print("hotspots%s   total weight %s in %d stacks   top %d by self and by incl" % (
        "  " + source if source else "", _num(total), len(stacks), top))
    print("  self%   incl%   max gain   frame")
    for r in chosen:
        frame = r["frame"] if len(r["frame"]) <= 140 else r["frame"][:139] + "…"
        print("%7.1f %7.1f %10s  %s   %s" % (r["self_pct"], r["incl_pct"], fmt_gain(r["max_gain"]),
                                          "✗" if r["gated"] else " ", frame))
    print("✗ = Amdahl gate: removing the frame entirely gives at most %.2fx (min-effect %.0f%%). "
          "Do not optimize it." % (1 + min_effect, min_effect * 100))
    return total, chosen


def read_folded(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return parse_folded(f.read(), strict=True)
    except OSError as e:
        raise MeasureError("cannot read %s: %s" % (path, e))


def cmd_hotspots(args):
    stacks = read_folded(args.file)
    if not stacks:
        raise MeasureError("%s contains no samples" % args.file)
    if args.json:
        total, rows = compute_hotspots(stacks, args.min_effect)
        print(json.dumps({"total": total, "min_effect": args.min_effect,
                          "rows": select_hotspots(rows, args.top)}, indent=2))
        return EXIT_OK
    print_hotspots(stacks, args.top, args.min_effect, args.file)
    return EXIT_OK


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

def detect_lang(cmd):
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
        toks.pop(0)
    if not toks:
        return "native"
    first = os.path.basename(toks[0])
    second = toks[1] if len(toks) > 1 else ""
    if re.match(r"^python(\d+(\.\d+)?)?$", first) or first == "pytest" \
            or (first in ("uv", "poetry") and second == "run"):
        return "python"
    if first in ("node", "npm", "npx", "pnpm", "bun"):
        return "node"
    if re.search(r"\bgo\s+test\b", cmd):
        return "go"
    return "native"


def _run_logged(argv, log_path, cwd=None, env=None):
    with open(log_path, "wb") as log:
        try:
            p = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=log, stderr=subprocess.STDOUT)
        except OSError as e:
            raise MeasureError("cannot run %s: %s" % (argv[0], e))
    with open(log_path, encoding="utf-8", errors="replace") as f:
        return p.returncode, f.read()


def _wrap_exit(cmd, rc_file):
    """Run cmd in sh and record its exit status in rc_file.

    Profilers do not reliably propagate the workload's exit code (py-spy can
    exit 1 after a successful run), so the shell writes it down.
    """
    return "%s\nmeasure_rc=$?\necho $measure_rc > %s\nexit $measure_rc" % (cmd, shlex.quote(rc_file))


def _read_rc(rc_file):
    v = _read(rc_file)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _fresh(path):
    if os.path.exists(path):
        os.remove(path)
    return path


def _workload_failed(rc, log):
    raise MeasureError("workload exited with code %s under the profiler\n--- last 20 lines of output ---\n%s"
                       % (rc, tail_lines(log)), EXIT_WORKLOAD)


def profile_python(cmd, prefix, rate, native, cwd):
    exe = shutil.which("py-spy")
    if not exe:
        raise MeasureError(FIXES["py-spy-missing"])
    raw = _fresh(os.path.abspath(prefix + ".pyspy.raw"))
    rc_file = _fresh(os.path.abspath(prefix + ".rc"))
    argv = [exe, "record", "--format", "raw", "--rate", str(rate), "--subprocesses",
            "--function", "-o", raw]
    if native:
        argv.append("--native")
    argv += ["--", "/bin/sh", "-c", _wrap_exit(cmd, rc_file)]
    rc, log = _run_logged(argv, prefix + ".log", cwd)
    wl_rc = _read_rc(rc_file)
    if wl_rc not in (None, 0):
        _workload_failed(wl_rc, log)
    if not os.path.exists(raw):
        if re.search(r"Permission denied|Operation not permitted|ptrace", log):
            raise MeasureError(FIXES["py-spy-perm"] + "\n--- py-spy output ---\n" + tail_lines(log))
        raise MeasureError("py-spy produced no profile (exit %d)\n%s" % (rc, tail_lines(log)))
    with open(raw, encoding="utf-8", errors="replace") as f:
        return normalize_pyspy(parse_folded(f.read(), strict=False))


def profile_native(cmd, prefix, rate, callgraph, cwd):
    if sys.platform == "darwin":
        raise MeasureError(FIXES["perf-macos"])
    exe = shutil.which("perf")
    if not exe:
        raise MeasureError(FIXES["perf-missing"])
    data = _fresh(os.path.abspath(prefix + ".perf.data"))
    rc_file = _fresh(os.path.abspath(prefix + ".rc"))
    cg = ["--call-graph", "dwarf"] if callgraph == "dwarf" else ["-g"]
    argv = [exe, "record", "-F", str(rate)] + cg + ["-o", data, "--", "/bin/sh", "-c",
                                                    _wrap_exit(cmd, rc_file)]
    rc, log = _run_logged(argv, prefix + ".log", cwd)
    if rc != 0 and re.search(r"perf_event_paranoid|Permission denied|Access to performance monitoring", log):
        raise MeasureError(FIXES["perf-perm"] + "\n--- perf output ---\n" + tail_lines(log))
    wl_rc = _read_rc(rc_file)
    if wl_rc not in (None, 0):
        _workload_failed(wl_rc, log)
    if rc != 0 or not os.path.exists(data):
        raise MeasureError("perf record failed (exit %d)\n%s" % (rc, tail_lines(log)))
    p = subprocess.run([exe, "script", "-i", data], capture_output=True, cwd=cwd)
    if p.returncode != 0:
        raise MeasureError("perf script failed\n" + tail_lines(p.stderr.decode("utf-8", "replace")))
    return collapse_perf(p.stdout.decode("utf-8", "replace"))


def profile_node(cmd, prefix, cwd):
    if not shutil.which("node"):
        raise MeasureError(FIXES["node-missing"])
    d = os.path.abspath(prefix + ".cpuprof.d")
    os.makedirs(d, exist_ok=True)
    for old in glob.glob(os.path.join(d, "*.cpuprofile")):
        os.remove(old)
    env = dict(os.environ)
    env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --cpu-prof --cpu-prof-dir=%s" % d).strip()
    rc, log = _run_logged(["/bin/sh", "-c", cmd], prefix + ".log", cwd, env)
    if rc != 0:
        _workload_failed(rc, log)
    files = glob.glob(os.path.join(d, "*.cpuprofile"))
    if not files:
        raise MeasureError("no .cpuprofile written to %s. Does the command start Node 12+?\n%s"
                           % (d, tail_lines(log)))
    with open(max(files, key=os.path.getsize)) as f:
        return collapse_cpuprofile(json.load(f))


def profile_go(cmd, prefix, rate, callgraph, cwd):
    if not re.search(r"\bgo\s+test\b", cmd):
        print("note: not a `go test` command; profiling the binary with perf instead.")
        return profile_native(cmd, prefix, rate, callgraph, cwd), "native"
    go = shutil.which("go")
    if not go:
        raise MeasureError(FIXES["go-missing"])
    pprof = _fresh(os.path.abspath(prefix + ".pprof"))
    rc, log = _run_logged(["/bin/sh", "-c", "%s -cpuprofile %s" % (cmd, shlex.quote(pprof))],
                          prefix + ".log", cwd)
    if rc != 0:
        _workload_failed(rc, log)
    if not os.path.exists(pprof):
        raise MeasureError("go test wrote no CPU profile. -cpuprofile needs exactly one package.\n"
                           + tail_lines(log))
    # pprof itself needs no cgo; without this, machines lacking gcc fail to build the tool.
    env = dict(os.environ, CGO_ENABLED="0")
    p = subprocess.run([go, "tool", "pprof", "-traces", pprof], capture_output=True, cwd=cwd, env=env)
    if p.returncode != 0:
        raise MeasureError("go tool pprof failed\n" + tail_lines(p.stderr.decode("utf-8", "replace")))
    return collapse_pprof_traces(p.stdout.decode("utf-8", "replace")), "go"


def cmd_profile(args):
    if not args.output:
        raise MeasureError("profile needs -o PREFIX")
    prefix = args.output[:-len(".folded")] if args.output.endswith(".folded") else args.output
    d = os.path.dirname(prefix)
    if d:
        os.makedirs(d, exist_ok=True)
    if args.from_file:
        if args.cmd:
            raise MeasureError("give either --from FILE or a command, not both")
        stacks, lang = sniff_and_collapse(args.from_file)
    else:
        if not args.cmd:
            raise MeasureError("profile needs a command after -- (or --from FILE)")
        if args.cwd and not os.path.isdir(args.cwd):
            raise MeasureError("directory not found: %s" % args.cwd)
        lang = detect_lang(args.cmd) if args.lang == "auto" else args.lang
        if lang == "python":
            stacks = profile_python(args.cmd, prefix, args.rate, args.native, args.cwd)
        elif lang == "node":
            stacks = profile_node(args.cmd, prefix, args.cwd)
        elif lang == "go":
            stacks, lang = profile_go(args.cmd, prefix, args.rate, args.callgraph, args.cwd)
        else:
            stacks = profile_native(args.cmd, prefix, args.rate, args.callgraph, args.cwd)
    if not stacks:
        raise MeasureError("the profile contains no samples. Is the workload long enough (>= 200 ms)?")
    folded = prefix + ".folded"
    with open(folded, "w") as f:
        f.write(format_folded(stacks))
    if args.json:
        total, rows = compute_hotspots(stacks, args.min_effect)
        print(json.dumps({"folded": folded, "lang": lang, "total": total,
                          "rows": select_hotspots(rows, args.top)}, indent=2))
        return EXIT_OK
    print("profile  lang=%s  wrote %s (%d stacks)" % (lang, folded, len(stacks)))
    print_hotspots(stacks, args.top, args.min_effect)
    return EXIT_OK


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

class Node(object):
    __slots__ = ("name", "value", "children")

    def __init__(self, name):
        self.name = name
        self.value = 0.0
        self.children = {}


def build_tree(stacks, scale):
    root = Node("all")
    for stack, c in stacks.items():
        v = c * scale
        root.value += v
        node = root
        for f in stack.split(";"):
            child = node.children.get(f)
            if child is None:
                child = node.children[f] = Node(f)
            child.value += v
            node = child
    return root


def path_values(root):
    out = {}
    work = [(root, ())]
    while work:
        node, path = work.pop()
        out[path] = node.value
        for name, child in node.children.items():
            work.append((child, path + (name,)))
    return out


def tree_depth(root):
    best = 0
    work = [(root, 1)]
    while work:
        node, d = work.pop()
        best = max(best, d)
        for child in node.children.values():
            work.append((child, d + 1))
    return best


def self_by_name(stacks, scale):
    out = {}
    for stack, c in stacks.items():
        leaf = stack.rsplit(";", 1)[-1]
        out[leaf] = out.get(leaf, 0.0) + c * scale
    return out


def change_ratio(a, b):
    m = max(a, b)
    return 0.0 if m <= 0 else (b - a) / m


def diff_color(d):
    """(fill, text color) for a relative change d in [-1, 1]."""
    if d < -0.05:
        light = 88 - 46 * min(1.0, -d)
        return "hsl(212,72%%,%.0f%%)" % light, ("#fff" if light < 58 else "#14202e")
    if d > 0.05:
        light = 88 - 44 * min(1.0, d)
        return "hsl(2,76%%,%.0f%%)" % light, ("#fff" if light < 58 else "#2e1414")
    return "hsl(38,42%,80%)", "#2b2418"


CHAR_PX = 6.6
ROW_PX = 17
SVG_WIDTH = 1200


def fit_label(name, width):
    if width <= 40:
        return ""
    chars = int((width - 6) / CHAR_PX)
    if chars < 2:
        return ""
    return name if len(name) <= chars else name[:chars - 1] + "…"


def render_flame_svg(tree, own, other, before, px, depth, fmtv, svg_id):
    """Bottom-up flame graph; each frame colored by its path's before→after change."""
    height = depth * ROW_PX
    parts = ['<svg id="%s" class="fg" xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d" data-width="%d" data-total-px="%.2f">'
             % (svg_id, SVG_WIDTH, height, SVG_WIDTH, height, SVG_WIDTH, tree.value * px)]
    work = [(tree, (), 0.0, 0)]
    while work:
        node, path, x, d = work.pop()
        w = node.value * px
        if w < 0.5:
            continue
        mine, theirs = own.get(path, 0.0), other.get(path, 0.0)
        a, b = (mine, theirs) if before else (theirs, mine)
        fill, ink = diff_color(change_ratio(a, b))
        y = (depth - 1 - d) * ROW_PX
        delta = b - a
        pct = ("%+.1f%%" % (100.0 * delta / a)) if a > 0 else "new"
        tip = "%s\nbefore: %s\nafter: %s\nΔ: %s (%s)" % (node.name, fmtv(a), fmtv(b),
                                                       fmtv(delta, True), pct)
        parts.append(
            '<g class="%s" data-x="%.3f" data-w="%.3f" data-d="%d" data-n="%s"><title>%s</title>'
            '<rect x="%.2f" y="%d" width="%.2f" height="%d" rx="1.5" fill="%s"/>'
            '<text x="%.2f" y="%.1f" fill="%s">%s</text></g>'
            % ("f root" if d == 0 else "f", x, w, d, html.escape(node.name), html.escape(tip),
               x, y, w, ROW_PX - 1, fill, x + 3, y + ROW_PX - 5, ink,
               html.escape(fit_label(node.name, w))))
        cx = x
        stacked = []
        for child in sorted(node.children.values(), key=lambda n: n.name):
            stacked.append((child, path + (child.name,), cx, d + 1))
            cx += child.value * px
        work.extend(reversed(stacked))
    parts.append("</svg>")
    return "".join(parts)


DIFF_CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6862;--line:#e2ded5;--card:#fff;--code:#f3f0e8}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#16171a;--fg:#e9e7e2;--muted:#9a978f;--line:#2c2e33;--card:#1d1f23;--code:#24262b}}
:root[data-theme="dark"]{--bg:#16171a;--fg:#e9e7e2;--muted:#9a978f;--line:#2c2e33;--card:#1d1f23;--code:#24262b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1248px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:20px;margin:0 0 12px}
h2{font-size:13px;margin:28px 0 8px;color:var(--muted);font-weight:600;letter-spacing:.04em;text-transform:uppercase}
pre,code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
pre.verdict{background:var(--code);border:1px solid var(--line);border-radius:6px;padding:10px 12px;white-space:pre-wrap;word-break:break-word;margin:0 0 12px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:0 0 12px}
dt{color:var(--muted)}dd{margin:0;word-break:break-word}
.note{text-transform:none;letter-spacing:0;font-weight:400}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:8px 0 0;color:var(--muted);font-size:13px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{display:inline-block;width:14px;height:12px;border-radius:2px}
.graph{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px}
svg.fg{display:block;width:100%;height:auto;font:11px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;cursor:pointer}
svg.fg g.f:hover rect{stroke:var(--fg);stroke-width:.8}
svg.fg text{pointer-events:none}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:right;padding:5px 10px;border-bottom:1px solid var(--line);white-space:nowrap;font-variant-numeric:tabular-nums}
th:last-child,td:last-child{text-align:left;white-space:normal;word-break:break-all}
th{color:var(--muted);font-weight:600}
td.down{color:hsl(212,70%,50%)}td.up{color:hsl(2,70%,54%)}
"""

DIFF_JS = """
(function(){
  var CH=%(char)s;
  function fit(n,w){if(w<=40)return'';var c=Math.floor((w-6)/CH);if(c<2)return'';return n.length<=c?n:n.slice(0,c-1)+'\\u2026';}
  document.querySelectorAll('svg.fg').forEach(function(svg){
    var W=+svg.dataset.width, gs=[].slice.call(svg.querySelectorAll('g.f'));
    function place(g,nx,nw){var r=g.querySelector('rect'),t=g.querySelector('text');
      r.setAttribute('x',nx);r.setAttribute('width',nw);t.setAttribute('x',nx+3);t.textContent=fit(g.dataset.n,nw);}
    function zoom(cx,cw,cd){gs.forEach(function(g){
      var x=+g.dataset.x,w=+g.dataset.w,d=+g.dataset.d,show;
      if(d<cd){show=x<=cx+1e-6&&x+w>=cx+cw-1e-6;if(show)place(g,0,W);}
      else{show=x>=cx-1e-6&&x+w<=cx+cw+1e-6;if(show)place(g,(x-cx)*W/cw,w*W/cw);}
      g.style.display=show?'':'none';});}
    function reset(){gs.forEach(function(g){g.style.display='';place(g,+g.dataset.x,+g.dataset.w);});}
    svg.addEventListener('click',function(e){var g=e.target.closest('g.f');if(!g)return;
      if(g.classList.contains('root'))reset();else zoom(+g.dataset.x,+g.dataset.w,+g.dataset.d);});
  });
})();
"""


def top_changes(a_stacks, b_stacks, sa, sb, n=15):
    """Top n frames by absolute change in self time, aggregated by frame name."""
    a_self, b_self = self_by_name(a_stacks, sa), self_by_name(b_stacks, sb)
    rows = []
    for name in set(a_self) | set(b_self):
        a, b = a_self.get(name, 0.0), b_self.get(name, 0.0)
        rows.append({"frame": name, "before": a, "after": b, "delta": b - a,
                     "delta_pct": (100.0 * (b - a) / a) if a > 0 else None})
    rows.sort(key=lambda r: (-abs(r["delta"]), r["frame"]))
    return rows[:n]


_LINE_SUFFIX = re.compile(r":\d+(\)?)$")


def strip_line_numbers(stacks):
    """'f (app.py:34)' -> 'f (app.py)', 'f app.js:10' -> 'f app.js'.

    An edit above a function shifts its line number; without this, the same
    function would show up as removed in before and new in after.
    """
    out = {}
    for stack, c in stacks.items():
        key = ";".join(_LINE_SUFFIX.sub(r"\1", f) for f in stack.split(";"))
        out[key] = out.get(key, 0) + c
    return out


def render_diff_html(a_stacks, b_stacks, a_time=None, b_time=None, meta=None):
    meta = meta or {}
    a_stacks, b_stacks = strip_line_numbers(a_stacks), strip_line_numbers(b_stacks)
    a_total, b_total = float(sum(a_stacks.values())), float(sum(b_stacks.values()))
    if a_total <= 0 or b_total <= 0:
        raise MeasureError("both profiles need samples")
    scaled = a_time is not None and b_time is not None
    if scaled:
        sa, sb = a_time / a_total, b_time / b_total

        def fmtv(v, signed=False):
            s = fmt_time(abs(v))
            return (("-" if v < 0 else "+") + s) if signed else s
    else:
        sa, sb = 100.0 / a_total, 100.0 / b_total

        def fmtv(v, signed=False):
            return ("%+.2f%%" if signed else "%.2f%%") % v
    ta, tb = build_tree(a_stacks, sa), build_tree(b_stacks, sb)
    pa, pb = path_values(ta), path_values(tb)
    px = SVG_WIDTH / max(ta.value, tb.value)
    depth = max(tree_depth(ta), tree_depth(tb))
    svg_a = render_flame_svg(ta, pa, pb, True, px, depth, fmtv, "before")
    svg_b = render_flame_svg(tb, pb, pa, False, px, depth, fmtv, "after")
    changes = top_changes(a_stacks, b_stacks, sa, sb)

    esc = html.escape
    head = []
    if meta.get("verdict_line"):
        head.append('<pre class="verdict">%s</pre>' % esc(meta["verdict_line"]))
    head.append("<dl>")
    if meta.get("workload"):
        head.append("<dt>workload</dt><dd><code>%s</code></dd>" % esc(meta["workload"]))
    head.append("<dt>env</dt><dd><code>%s</code></dd>" % esc(meta.get("env") or env_line()))
    head.append("<dt>profiles</dt><dd><code>%s</code> → <code>%s</code></dd>"
                % (esc(meta.get("a_name", "before")), esc(meta.get("b_name", "after"))))
    if scaled:
        head.append("<dt>scale</dt><dd>width = time: each profile is scaled to its median wall time "
                    "(%s → %s), so a real speedup shows as a shorter after graph.</dd>"
                    % (fmt_time(a_time), fmt_time(b_time)))
    else:
        head.append("<dt>scale</dt><dd><strong>normalized</strong>: no timing was given, so each "
                    "profile is scaled to 100%. Width shows share of samples, not absolute time.</dd>")
    head.append("</dl>")
    head.append('<div class="legend">'
                '<span><i style="background:hsl(212,72%,42%)"></i>shrank (full blue: only in before)</span>'
                '<span><i style="background:hsl(2,76%,44%)"></i>grew (full red: only in after)</span>'
                '<span><i style="background:hsl(38,42%,80%)"></i>within ±5%</span>'
                '<span>click a frame to zoom, click the bottom bar to reset</span></div>')

    rows = []
    for r in changes:
        cls = "down" if r["delta"] < 0 else ("up" if r["delta"] > 0 else "")
        pct = "new" if r["delta_pct"] is None else "%+.1f%%" % r["delta_pct"]
        rows.append('<tr><td>%s</td><td>%s</td><td class="%s">%s</td><td class="%s">%s</td><td>%s</td></tr>'
                    % (esc(fmtv(r["before"])), esc(fmtv(r["after"])), cls, esc(fmtv(r["delta"], True)),
                       cls, esc(pct), esc(r["frame"])))

    doc = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Diff flame graph</title><style>%(css)s</style></head>
<body><main>
<h1>Diff flame graph</h1>
%(head)s
<h2>Before</h2><div class="graph">%(svg_a)s</div>
<h2>After <span class="note">(same x scale)</span></h2><div class="graph">%(svg_b)s</div>
<h2>Top %(n)d changes by self time</h2>
<div class="tablewrap"><table><thead><tr><th>before</th><th>after</th><th>Δ</th><th>Δ%%</th><th>frame</th></tr></thead>
<tbody>%(rows)s</tbody></table></div>
</main><script>%(js)s</script></body></html>
""" % {"css": DIFF_CSS, "head": "\n".join(head), "svg_a": svg_a, "svg_b": svg_b,
       "n": len(changes), "rows": "\n".join(rows), "js": DIFF_JS % {"char": CHAR_PX}}
    return doc, changes, fmtv, scaled


def cmd_diff(args):
    a_stacks, b_stacks = read_folded(args.base), read_folded(args.cand)
    meta = {"a_name": args.base, "b_name": args.cand}
    a_time, b_time = args.a_time, args.b_time
    if (a_time is None) != (b_time is None):
        raise MeasureError("--a-time and --b-time must be given together")
    if args.compare:
        try:
            with open(args.compare) as f:
                cmp = json.load(f)
        except (OSError, ValueError) as e:
            raise MeasureError("cannot read compare JSON %s: %s" % (args.compare, e))
        meta["verdict_line"] = cmp.get("verdict_line")
        meta["workload"] = cmp.get("a", {}).get("cmd")
        meta["env"] = cmp.get("env")
        if a_time is None and cmp.get("metric") == "wall_time_s":
            a_time = cmp["a"]["summary"]["median"]
            b_time = cmp["b"]["summary"]["median"]
    doc, changes, fmtv, scaled = render_diff_html(a_stacks, b_stacks, a_time, b_time, meta)
    d = os.path.dirname(args.output)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(doc)
    if args.json:
        print(json.dumps({"html": args.output, "scaled_to_time": scaled, "a_time": a_time,
                          "b_time": b_time, "changes": changes}, indent=2))
        return EXIT_OK
    if scaled:
        print("diff  scaled to median wall time: before %s → after %s" % (fmt_time(a_time), fmt_time(b_time)))
    else:
        print("diff  normalized to 100% per profile (no timing given: width = share, not time)")
    if meta.get("verdict_line"):
        print(meta["verdict_line"])
    print("top %d changes by self time" % len(changes))
    print("  %10s %10s %11s %8s   frame" % ("before", "after", "Δ", "Δ%"))
    for r in changes:
        pct = "new" if r["delta_pct"] is None else "%+.1f%%" % r["delta_pct"]
        print("  %10s %10s %11s %8s   %s" % (fmtv(r["before"]), fmtv(r["after"]),
                                           fmtv(r["delta"], True), pct, r["frame"]))
    print("wrote " + args.output)
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def split_command(argv):
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def join_command(tokens):
    """One token is used verbatim (shell syntax allowed); several are shell-quoted."""
    if not tokens:
        return None
    if len(tokens) == 1:
        return tokens[0]
    return " ".join(shlex.quote(t) for t in tokens)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON instead of text")

    p = argparse.ArgumentParser(
        prog="measure", description="Profile before you optimize. Prove it or revert it.",
        epilog="Everything after -- is the workload command, run through sh -c. "
               "Exit codes: 0 ok/IMPROVED, 1 error, 2 NO_EFFECT/INCONCLUSIVE, 3 REGRESSED, "
               "4 workload failed, 5 outputs differ (--strict-output).")
    p.add_argument("--version", action="version", version="measure " + VERSION)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("doctor", parents=[common], help="check benchmarking noise and profiler access")

    b = sub.add_parser("bench", parents=[common], help="time one command")
    b.add_argument("--runs", type=int, default=15)
    b.add_argument("--warmup", type=int, default=2)
    b.add_argument("--metric", choices=["time", "rss"], default="time",
                   help="time = wall clock (default), rss = peak resident memory in MiB")
    b.add_argument("--parse", metavar="REGEX", help="metric = float in capture group 1 of stdout/stderr")
    b.add_argument("--higher-better", action="store_true")
    b.add_argument("--timeout", type=float, metavar="S")
    b.add_argument("--cwd", metavar="DIR")
    b.add_argument("-o", dest="output", metavar="OUT.json")

    c = sub.add_parser("compare", parents=[common], help="interleaved A/B benchmark with a verdict")
    c.add_argument("--a", metavar="CMD")
    c.add_argument("--b", metavar="CMD")
    c.add_argument("--a-cwd", metavar="DIR")
    c.add_argument("--b-cwd", metavar="DIR")
    c.add_argument("--runs", type=int, default=15, help="pairs to run (default 15)")
    c.add_argument("--warmup", type=int, default=2)
    c.add_argument("--min-effect", type=float, default=0.02)
    c.add_argument("--until-conclusive", action="store_true")
    c.add_argument("--max-runs", type=int, default=60, help="pair budget for --until-conclusive")
    c.add_argument("--metric", choices=["time", "rss"], default="time",
                   help="time = wall clock (default), rss = peak resident memory in MiB")
    c.add_argument("--parse", metavar="REGEX", help="metric = float in capture group 1 of stdout/stderr")
    c.add_argument("--higher-better", action="store_true")
    c.add_argument("--seed", type=int)
    c.add_argument("--strict-output", action="store_true")
    c.add_argument("--ignore-line", action="append", metavar="REGEX")
    c.add_argument("--timeout", type=float, metavar="S")
    c.add_argument("-o", dest="output", metavar="OUT.json")

    pr = sub.add_parser("profile", parents=[common], help="sample a command into PREFIX.folded")
    pr.add_argument("--lang", choices=["auto", "python", "native", "node", "go"], default="auto")
    pr.add_argument("--rate", type=int, default=999)
    pr.add_argument("--callgraph", choices=["fp", "dwarf"], default="fp")
    pr.add_argument("--native", action="store_true", help="py-spy --native (C extensions)")
    pr.add_argument("--from", dest="from_file", metavar="FILE")
    pr.add_argument("--cwd", metavar="DIR")
    pr.add_argument("--top", type=int, default=15)
    pr.add_argument("--min-effect", type=float, default=0.02)
    pr.add_argument("-o", dest="output", metavar="PREFIX")

    h = sub.add_parser("hotspots", parents=[common], help="self/incl table with the Amdahl gate")
    h.add_argument("file", metavar="FILE.folded")
    h.add_argument("--top", type=int, default=15)
    h.add_argument("--min-effect", type=float, default=0.02)

    d = sub.add_parser("diff", parents=[common], help="before/after diff flame graph (HTML)")
    d.add_argument("base", metavar="BASE.folded")
    d.add_argument("cand", metavar="CAND.folded")
    d.add_argument("--compare", metavar="cmp.json")
    d.add_argument("--a-time", type=float, metavar="S")
    d.add_argument("--b-time", type=float, metavar="S")
    d.add_argument("-o", dest="output", metavar="diff.html", required=True)
    return p


COMMANDS = {"doctor": cmd_doctor, "bench": cmd_bench, "compare": cmd_compare,
            "profile": cmd_profile, "hotspots": cmd_hotspots, "diff": cmd_diff}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    opts, cmd_tokens = split_command(argv)
    parser = build_parser()
    args = parser.parse_args(opts)
    if not args.command:
        parser.print_help()
        return EXIT_ERROR
    args.cmd = join_command(cmd_tokens)
    if args.cmd and args.command in ("doctor", "hotspots", "diff"):
        parser.error("%s does not take a command after --" % args.command)
    try:
        return COMMANDS[args.command](args)
    except MeasureError as e:
        print("error: %s" % e, file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
