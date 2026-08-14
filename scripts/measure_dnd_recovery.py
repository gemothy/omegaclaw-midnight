#!/usr/bin/env python3
"""How long after the world refuses a send does that same person accept one?

This sets _REFUSAL_TTL_MS["dnd"], which is otherwise a number somebody picked.
It is worth re-running whenever the city's behaviour changes, because two
decisions rest on it:

  the backoff base      a first retry that fires before anybody has ever been
                        observed to recover is a guaranteed-wasted write and turn
  a fast retry          tempting, because an inbound thread dies at 60s - but
                        only worth doing if people recover inside that window

Measured 2026-08-14 over three hours: 10 refusal->success pairs, minimum 561s,
median 1553s, NONE under 60s. So the base moved 300000 -> 600000, and the fast
retry inside the thread window was not built.

    python3 scripts/measure_dnd_recovery.py [window]

Purely observational - it reads the log and sends nothing. Deliberately: probing
by writing to real agents would be experimenting on a shared world with this
agent's own voice.
"""
import collections
import datetime
import re
import sys

sys.path.insert(0, "scripts")
from dockerlogs import read_window                      # noqa: E402


def stamp(line):
    m = re.match(r"(2026-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)", line)
    return datetime.datetime.fromisoformat(m.group(1)) if m else None


def main():
    window = sys.argv[1] if len(sys.argv) > 1 else "180m"
    # A big tail on purpose: pairs are rare, and the default 4000 lines covers
    # far less than the window asked for.
    text, error = read_window("omegaclaw", window, tail=40000, strip_ts=False)
    if error:
        print(f"note: {error}\n")
    lines = (text or "").splitlines()

    refused = collections.defaultdict(list)
    delivered = collections.defaultdict(list)
    for line in lines:
        when = stamp(line)
        if not when:
            continue
        ids = re.findall(r"(user-agent-[\w-]+)", line)
        if not ids:
            continue
        low = line.lower()
        if "do not disturb" in low or "do-not-disturb" in low:
            for who in ids:
                refused[who].append(when)
        if "MCITY-SPEAK-OK" in line:
            # Prefer the to= field the success line now carries. Falling back to
            # "any id on the line" is what made this undercount: until to=
            # existed, a delivery named a thread and a message and nobody, so
            # this saw 4 people delivered to against 43 refused. Where to= is
            # absent the line is from before that change, and the fallback keeps
            # older windows readable rather than silently reading zero.
            named = re.search(r"\bto=(user-agent-[\w-]+)", line)
            for who in ([named.group(1)] if named else ids):
                delivered[who].append(when)

    gaps = []
    for who, times in refused.items():
        for t0 in times:
            later = [d for d in delivered.get(who, []) if d > t0]
            if later:
                gaps.append((min(later) - t0).total_seconds())
    gaps.sort()

    print(f"{len(lines)} lines, {len(refused)} people refused, "
          f"{len(delivered)} people delivered to")
    if not gaps:
        print("\nno refusal->success pair for the same person in this window. "
              "Widen it; this needs hours, not minutes.")
        return 0
    print(f"\n{len(gaps)} refusal -> next success pairs, same person")
    print(f"  fastest {gaps[0]:.0f}s   median {gaps[len(gaps) // 2]:.0f}s   "
          f"slowest {gaps[-1]:.0f}s")
    print(f"  recovered inside the 60s thread window: "
          f"{sum(1 for g in gaps if g < 60)}")
    print(f"\nthe dnd backoff base should sit just above the fastest recovery. "
          f"It is currently 600s; fastest here is {gaps[0]:.0f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
