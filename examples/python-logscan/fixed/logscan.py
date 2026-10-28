"""logscan: summarize a log file by message template.

Usage: python3 logscan.py data.log

Prints the date, line and level counts, the error rate, the log span and rate, the
busiest second and the most frequent message templates (variable parts such
as ids, IPs and durations are masked so that similar lines group together).
"""
import re
import sys
from datetime import datetime, timezone

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

MASKS = [
    (r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>"),
    (r"\b[0-9a-f]{8}\b", "<hex>"),
    (r"\d+", "<n>"),
]

_TS_CACHE = {}


def parse_ts(date, clock):
    """('2026-10-27', '08:01:12') -> epoch seconds (UTC). strptime is slow, so cache it."""
    stamp = date + " " + clock
    if len(stamp) != 19:
        raise ValueError("bad timestamp: %r" % stamp)
    if stamp not in _TS_CACHE:
        _TS_CACHE[stamp] = datetime.strptime(stamp, TS_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    return _TS_CACHE[stamp]


_COMPILED_MASKS = [(re.compile(pattern, re.IGNORECASE), token) for pattern, token in MASKS]


def normalize(msg):
    """Mask the variable parts of a message so lines group by template."""
    for rx, token in _COMPILED_MASKS:
        msg = rx.sub(token, msg)
    return msg


def dedupe(templates):
    """Unique templates in first-seen order."""
    return list(dict.fromkeys(templates))


def render_report(stats, uniq, counts):
    # Report layout. Selection of the top templates, column widths and padding
    # are all done by hand so the output does not depend on any library.
    picked = []
    used = {}
    k = 0
    while k < 15 and k < len(uniq):
        best = -1
        best_i = -1
        i = 0
        while i < len(uniq):
            t = uniq[i]
            if used.get(t) is None:
                c = counts[t]
                if c > best or (c == best and t < uniq[best_i]):
                    best = c
                    best_i = i
            i = i + 1
        picked.append([uniq[best_i], best])
        used[uniq[best_i]] = 1
        k = k + 1
    width = 0
    for p in picked:
        if len(p[0]) > width:
            width = len(p[0])
    out = []
    out.append("date:       " + stats["date"])
    out.append("lines:      " + str(stats["lines"]))
    lv = ""
    for name in sorted(stats["levels"].keys()):
        if lv != "":
            lv = lv + "  "
        lv = lv + name + "=" + str(stats["levels"][name])
    out.append("levels:     " + lv)
    errors = stats["levels"].get("ERROR", 0)
    pct = str(round(100.0 * errors / stats["lines"], 2)) if stats["lines"] else "0"
    out.append("error rate: " + pct + "%")
    span = stats["last"] - stats["first"]
    rate = str(round(stats["lines"] / span, 1)) if span > 0 else "-"
    out.append("span:       " + str(int(span)) + "s (" + rate + " lines/s)")
    out.append("busiest:    " + stats["busiest"] + " (" + str(stats["busiest_lines"]) + " lines)")
    out.append("templates:  " + str(len(uniq)) + " unique")
    out.append("")
    header = "count".rjust(7) + "  " + "template"
    out.append(header)
    rule = ""
    for _ in range(7 + 2 + width):
        rule = rule + "-"
    out.append(rule)
    for p in picked:
        c = str(p[1])
        while len(c) < 7:
            c = " " + c
        out.append(c + "  " + p[0])
    return "\n".join(out)


def scan(path):
    levels = {}
    per_second = {}
    counts = {}
    templates = []
    first = last = started = None
    with open(path) as f:
        for line in f:
            date, clock, level, host, msg = line.rstrip("\n").split(" ", 4)
            ts = parse_ts(date, clock)
            if first is None:
                first, started = ts, datetime.strptime(date + " " + clock, TS_FORMAT)
            last = ts
            per_second[ts] = per_second.get(ts, 0) + 1
            levels[level] = levels.get(level, 0) + 1
            tpl = normalize(msg)
            templates.append(tpl)
            counts[tpl] = counts.get(tpl, 0) + 1
    uniq = dedupe(templates)
    top = min(per_second, key=lambda s: (-per_second[s], s))
    stats = {"lines": len(templates), "levels": levels, "first": first or 0, "last": last or 0,
             "date": started.strftime("%a %d %b %Y") if started else "-",
             "busiest": datetime.fromtimestamp(top, timezone.utc).strftime("%H:%M:%S UTC"),
             "busiest_lines": per_second[top]}
    return render_report(stats, uniq, counts)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 logscan.py data.log")
    print(scan(sys.argv[1]))
