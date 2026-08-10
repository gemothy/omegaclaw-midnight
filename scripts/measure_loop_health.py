#!/usr/bin/env python3
"""Measure whether the agent loop is doing useful work or spinning on reads.

The failure this exists to catch: the agent burns turns re-reading lists that
were truncated before it could act on them, never reaching the steps of its
procedure that change the world.

Read skills are not free: each one spends a turn. A healthy loop keeps the read
ratio well under 100% and actually reaches its acting skills.

Counting caveat, learned the hard way. An earlier version of this script scanned
the whole log for `(mcity-...`, which also matched skill REGISTRATION and the
SKILLS catalogue echoed into every prompt. That inflated the counts by orders of
magnitude and produced a "93% redundant reads" figure that was an artefact, not a
measurement. Only lines matching _DECISION_RE - the model's actual chosen actions
- are counted now. Treat any pre-correction figure quoted elsewhere as void.

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

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from dockerlogs import read_window

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

# Only the model's chosen actions count. A naive scan over the whole log also
# matches skill REGISTRATION (`add-skill mcity-...`) and the SKILLS catalogue
# echoed in every prompt, which a restart injects by the hundred - that inflated
# a 6-minute window to 7,266 "reads". The agent's decisions appear as
#   ... | loop | (RESPONSE: ((mcity-threads) (mcity-agents)))
# whereas skill RESULTS come back as `(RESPONSE: (RESULTS:`, which is not a
# decision and must not be counted either.
_DECISION_RE = re.compile(r"\(RESPONSE: \((?!RESULTS:)(.*)$")


def _logs(container, since, timeout):
    """Windowed logs via our own clock.

    `docker logs --since` is evaluated against the DAEMON's clock, which on this
    host runs four hours behind the container, so it returned zero lines while
    the agent logged every second - silently turning this measurement into
    "0 skill calls". See scripts/dockerlogs.py.
    """
    return read_window(container, since, timeout=timeout)


def measure(text):
    decisions = "\n".join(m.group(1) for m in
                          (_DECISION_RE.search(line) for line in text.splitlines())
                          if m)
    counts = collections.Counter(_SKILL_RE.findall(decisions))
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
