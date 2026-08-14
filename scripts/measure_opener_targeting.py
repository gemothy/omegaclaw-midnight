#!/usr/bin/env python3
"""Does opening a conversation with the RIGHT person actually pay?

_best_person_to_talk_to ranks candidates in three tiers - somebody who once wrote
to us first, then somebody we have delivered to before, then everyone else - and
_WROTE_TO_US_TTL_MS decides how long the first tier remembers. Both were reasoned
about and neither had been checked against an outcome.

This asks the world instead. Purely from the thread list, no log and no
instrumentation: for every conversation WE opened, had that person written to us
first beforehand, and did they answer?

    python3 scripts/measure_opener_targeting.py

Measured 2026-08-14 over 11.7h of held threads:

    stranger                    7/45   15%
    wrote to us within 1h       3/ 7   42%
    wrote to us over 1h ago     0/ 2    0%

Two conclusions, one of them a change NOT made. The tiering pays - about
2.8x - so it stays. And extending _WROTE_TO_US_TTL_MS past an hour looked
obviously right until this split the tier by age: the evidence past the TTL is
the worst of the three groups, not the second best. n=2 decides nothing, but
the burden is on the change, and there was none for it.

Re-run before touching either. The counts are small and the city changes.
"""
import collections
import sys

sys.path.insert(0, "scripts")
from reply_funnel import live_threads, AGENT              # noqa: E402

TTL_HOURS = 1.0          # mirrors _WROTE_TO_US_TTL_MS


def main():
    rows = live_threads(200)
    if not rows:
        print("no threads from the live world - is the agent connected?")
        return 1

    # The first time each person opened a conversation with US.
    first_inbound = {}
    for row in rows:
        if row.get("initiatorAgentId") == AGENT:
            continue
        who = row.get("initiatorAgentId")
        created = row.get("threadCreatedAtMs") or 0
        first_inbound[who] = min(first_inbound.get(who, created), created)

    ours = [r for r in rows if r.get("initiatorAgentId") == AGENT]
    if not ours:
        print("we have not opened anything in the held history")
        return 0

    seen, won = collections.Counter(), collections.Counter()
    for row in ours:
        who = row.get("recipientAgentId")
        created = row.get("threadCreatedAtMs") or 0
        knew = who in first_inbound and first_inbound[who] < created
        if not knew:
            bucket = "stranger"
        else:
            hours = (created - first_inbound[who]) / 3600000.0
            bucket = (f"wrote to us within {TTL_HOURS:g}h" if hours <= TTL_HOURS
                      else f"wrote to us over {TTL_HOURS:g}h ago")
        seen[bucket] += 1
        if (row.get("recipientMessageCount") or 0) > 0:
            won[bucket] += 1

    span = max((r.get("threadCreatedAtMs") or 0) for r in rows)
    low = min((r.get("threadCreatedAtMs") or 0) for r in rows)
    print(f"{len(ours)} openers across {(span - low) / 3600000.0:.1f}h of held "
          f"threads\n")
    for bucket, n in seen.most_common():
        print(f"  {bucket:28} {won[bucket]:3}/{n:3}  "
              f"({int(100 * won[bucket] / n) if n else 0}% answered)")
    print("\nSmall counts. Treat a difference under about 2x as noise, and "
          "re-run before changing the tiers or the TTL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
