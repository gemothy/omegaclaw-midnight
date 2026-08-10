#!/usr/bin/env python3
"""Measure whether the agent loop is doing useful work or spinning on reads.

The failure this exists to catch: the agent burns nearly every turn re-reading
lists that were truncated before it could act on them, never reaching the steps
of its procedure that change the world. Measured on the unfixed harness:

    mcity-threads 316, mcity-agents 212, mcity-work 28, mcity-eat 4,
    mcity-trade 0  ->  93% of skill calls were redundant reads,
    while the agent was starving holding 10,764 meme_coin.

Read skills are not free: each one spends a turn. A healthy loop keeps the read
ratio well under 100% and actually reaches its acting skills.

    python3 scripts/measure_loop_health.py --since 40m
    python3 scripts/measure_loop_health.py --since 1h --json

Exit 0 always (this is a measurement, not a gate) unless the container is
unreachable, which exits 2.
"""

import argparse
import collections
import json
import re
import subprocess
import sys

# Skills that only observe. Everything else changes the world or communicates.
READ_SKILLS = (
    "mcity-threads", "mcity-thread", "mcity-agents", "mcity-needs",
    "mcity-inventory", "mcity-context", "mcity-areas", "mcity-merchants",
    "mcity-recent-events", "mcity-status", "mcity-inventory",
)
ACT_SKILLS = (
    "mcity-speak", "mcity-work", "mcity-eat", "mcity-trade", "mcity-harvest",
    "mcity-move-area", "send",
)

_SKILL_RE = re.compile(r"\((mcity-[a-z-]+|send)\b")


def _logs(container, since, timeout):
    try:
        proc = subprocess.run(
            ["docker", "logs", "--since", since, container],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return None, "docker not found on PATH"
    except subprocess.TimeoutExpired:
        return None, f"docker logs timed out after {timeout}s"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return None, detail[0] if detail else f"container {container!r} unavailable"
    return (proc.stdout or "") + (proc.stderr or ""), None


def measure(text):
    counts = collections.Counter(_SKILL_RE.findall(text))
    reads = sum(counts[s] for s in READ_SKILLS)
    acts = sum(counts[s] for s in ACT_SKILLS)
    total = reads + acts
    return {
        "counts": dict(counts.most_common()),
        "reads": reads,
        "acts": acts,
        "total": total,
        "read_ratio": round(reads / total, 4) if total else 0.0,
        "iterations": len(re.findall(r"-+iteration \d+", text)),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--container", default="omegaclaw")
    ap.add_argument("--since", default="30m", help="docker logs --since value")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    text, err = _logs(args.container, args.since, args.timeout)
    if err is not None:
        print(f"unavailable: {err}", file=sys.stderr)
        return 2

    result = measure(text)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"window: --since {args.since}   loop iterations: {result['iterations']}")
    if not result["total"]:
        print("no skill calls observed in this window")
        return 0
    print(f"\n{'skill':<22}{'calls':>7}")
    for skill, n in result["counts"].items():
        kind = "read" if skill in READ_SKILLS else "act"
        print(f"  {skill:<20}{n:>7}   {kind}")
    print(f"\nreads {result['reads']}  acts {result['acts']}  "
          f"read ratio {result['read_ratio']:.1%}")
    if result["read_ratio"] > 0.80:
        print("\nHIGH READ RATIO: the loop is mostly re-reading, not acting.")
        print("That is the signature of results being truncated before use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
