#!/usr/bin/env python3
"""What the agent's conversations actually look like, by the hour.

Every judgement about whether this harness is improving socially has been made
from an eight minute window, and those numbers swing wildly: two-way threads read
1, then 8, then 3, then 0, with no change between them that explains it. That is
not a trend, it is sampling noise, and tuning against it is how you chase your own
tail.

The thread list is already a history - each row carries threadCreatedAtMs and the
two message counts - and the endpoint pages backwards. So one paged read gives
hours of ground truth without waiting hours to collect it.

    python3 scripts/conversation_trend.py [hours]

Counts, per hour:
  opened-by-us / answered   how many strangers actually replied to us
  opened-by-them / answered how many people we did not leave on read
  two-way                   threads where both sides spoke, the only number that
                            really says a conversation happened
"""
import collections
import json
import subprocess
import sys
import time

AGENT = "user-agent-ow0v8z9lg4v5kyr"
BASE = "http://localhost:8080/mcity/api"
PAGE = 50


def fetch(path):
    out = subprocess.run(
        ["docker", "exec", "omegaclaw", "sh", "-c",
         f"curl -s -m 25 '{BASE}/{path}'"],
        capture_output=True, text=True, timeout=90)
    try:
        return json.loads(out.stdout)
    except Exception:
        return {}


def all_threads(hours):
    """Page backwards until the rows fall outside the window."""
    cutoff = time.time() * 1000 - hours * 3600_000
    seen, cursor, rows = set(), None, []
    for _ in range(20):                     # a page is 50; 1000 threads is plenty
        path = f"agents/{AGENT}/threads?limit={PAGE}"
        if cursor:
            path += f"&beforeLatestMessageId={cursor}"
        payload = fetch(path)
        page = payload.get("threads") or []
        if not page:
            break
        fresh = [t for t in page if t.get("threadId") not in seen]
        if not fresh:
            break
        for t in fresh:
            seen.add(t.get("threadId"))
        rows.extend(fresh)
        oldest = min((t.get("threadCreatedAtMs") or 0) for t in fresh)
        cursor = (payload.get("page") or {}).get("nextBeforeLatestMessageId")
        if not cursor or oldest < cutoff:
            break
    return [t for t in rows if (t.get("threadCreatedAtMs") or 0) >= cutoff]


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    rows = all_threads(hours)
    if not rows:
        print("no threads in that window")
        return 0
    buckets = collections.defaultdict(lambda: collections.Counter())
    now = time.time() * 1000
    for t in rows:
        age_h = int((now - (t.get("threadCreatedAtMs") or 0)) // 3600_000)
        b = buckets[age_h]
        ours = t.get("initiatorAgentId") == AGENT
        theirs_spoke = (t.get("recipientMessageCount") if ours
                        else t.get("initiatorMessageCount")) or 0
        we_spoke = (t.get("initiatorMessageCount") if ours
                    else t.get("recipientMessageCount")) or 0
        b["opened_us" if ours else "opened_them"] += 1
        if ours and theirs_spoke:
            b["they_answered"] += 1
        if not ours and we_spoke:
            b["we_answered"] += 1
        if theirs_spoke and we_spoke:
            b["two_way"] += 1

    print(f"{'hours ago':>9}  {'we opened':>9} {'answered':>8}   "
          f"{'they opened':>11} {'answered':>8}   {'two-way':>7}")
    total = collections.Counter()
    for age in sorted(buckets):
        b = buckets[age]
        total.update(b)
        print(f"{age:>9}  {b['opened_us']:>9} {b['they_answered']:>8}   "
              f"{b['opened_them']:>11} {b['we_answered']:>8}   {b['two_way']:>7}")
    print(f"{'TOTAL':>9}  {total['opened_us']:>9} {total['they_answered']:>8}   "
          f"{total['opened_them']:>11} {total['we_answered']:>8}   "
          f"{total['two_way']:>7}")
    opened_them = total["opened_them"] or 1
    opened_us = total["opened_us"] or 1
    print(f"\nwe answer {100 * total['we_answered'] // opened_them}% of people "
          f"who write to us; {100 * total['they_answered'] // opened_us}% of the "
          "people we write to answer back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
