"""Generate a synthetic log file for the logscan demo (fixed seed, deterministic).

Usage: python3 gen.py [-n 120000] [--seed 42] [--seconds 3] [-o data.log]

Each line: "YYYY-mm-dd HH:MM:SS LEVEL host message"
"""
import argparse
import random

SUBJECTS = ["user", "session", "order", "invoice", "cart", "token", "payment", "upload",
            "job", "report", "cache", "queue", "device", "account", "webhook", "export",
            "coupon", "ticket", "backup", "license"]
ACTIONS = [
    "created", "updated", "deleted", "expired", "failed to load",
    "timed out after {dur}", "retried {n} times", "rejected from {ip}",
    "locked by {hex}", "synced to node {n} in {dur}", "queued at position {n}",
    "throttled for {dur}", "migrated to shard {n}", "flagged by rule {hex}",
    "archived", "restored from snapshot {hex}",
]
LEVELS = [("INFO", 80), ("WARN", 12), ("DEBUG", 6), ("ERROR", 2)]


def hex_id(rng):
    while True:
        s = "%08x" % rng.getrandbits(32)
        if any(c.isalpha() for c in s):
            return s


def message(rng):
    subject = rng.choice(SUBJECTS)
    action = rng.choice(ACTIONS).format(
        dur="%dms" % rng.randint(1, 5000),
        n=rng.randint(1, 99),
        ip="10.%d.%d.%d" % (rng.randint(0, 255), rng.randint(0, 255), rng.randint(1, 254)),
        hex=hex_id(rng),
    )
    return "%s %d %s" % (subject, rng.randint(1, 999999), action)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=120000, help="number of lines")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--seconds", type=int, default=3, help="distinct timestamps (log span)")
    ap.add_argument("-o", default="data.log")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    levels = [name for name, weight in LEVELS for _ in range(weight)]
    with open(args.o, "w") as f:
        for i in range(args.n):
            t = 8 * 3600 + i * args.seconds // args.n  # starts 08:00:00
            hh, rem = divmod(t % 86400, 3600)
            mm, ss = divmod(rem, 60)
            f.write("2026-10-27 %02d:%02d:%02d %s api-%d %s\n" % (
                hh, mm, ss, rng.choice(levels), rng.randint(1, 8), message(rng)))


if __name__ == "__main__":
    main()
