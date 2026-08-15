#!/usr/bin/env python3
"""One comparable snapshot of whether the agent is doing its job.

check_all answers "is anything broken". This answers "is it working", which is a
different question and the one that decides whether a pass was worth running.

Everything here comes from the WORLD's own record - the thread list it keeps
about us - not from the log. Four wrong findings this session came from inferring
behaviour out of log regexes, and the numbers that held up were always the ones
the world could confirm. The two log-derived figures are labelled as such.

The windows are FIXED, so two runs are comparable. Quoting 45m one pass and 90m
the next made every trend unreadable, which is most of why this exists.

    python3 scripts/state_of_play.py

Never fails. It is a report, not a check.
"""
import calendar
import collections
import subprocess
import sys
import time

sys.path.insert(0, "scripts")
from reply_funnel import live_threads, AGENT               # noqa: E402
from dockerlogs import read_window, count_results          # noqa: E402

HOUR = 3600000


def pct(n, d):
    return f"{n}/{d} ({int(100 * n / d)}%)" if d else f"{n}/0 (-)"


def main():
    rows = live_threads(200)
    if not rows:
        print("no threads from the live world - is the agent connected?")
        return 0
    now = int(time.time() * 1000)
    inbound = [t for t in rows if t.get("initiatorAgentId") != AGENT]
    ours = [t for t in rows if t.get("initiatorAgentId") == AGENT]

    span = (now - min((t.get("threadCreatedAtMs") or now) for t in rows)) / HOUR
    print(f"the world still holds {len(rows)} threads, spanning {span:.1f}h\n")

    print("ANSWERING - the metric the harness can actually move")
    for label, hours in (("last 1h", 1), ("last 3h", 3), ("all held", 999)):
        sel = [t for t in inbound
               if (t.get("threadCreatedAtMs") or 0) >= now - hours * HOUR]
        got = sum(1 for t in sel if (t.get("recipientMessageCount") or 0) > 0)
        print(f"  {label:9} {pct(got, len(sel)):>14}  people we answered")

    print("\nBEING ANSWERED - what the city thinks of what we say")
    for label, hours in (("last 3h", 3), ("all held", 999)):
        sel = [t for t in ours
               if (t.get("threadCreatedAtMs") or 0) >= now - hours * HOUR]
        got = sum(1 for t in sel if (t.get("recipientMessageCount") or 0) > 0)
        print(f"  {label:9} {pct(got, len(sel)):>14}  openers answered")
    two_way = sum(1 for t in rows
                  if (t.get("initiatorMessageCount") or 0) > 0
                  and (t.get("recipientMessageCount") or 0) > 0)
    print(f"  {'two-way':9} {two_way:>14}  threads where both sides spoke")

    # Distinctness of what we said. Templating is the failure the mission names -
    # "a line that would fit any stranger" - and it is cheap to watch here.
    said = [(t.get("latestMessagePreview") or "").strip() for t in ours]
    said = [s for s in said if s]
    if said:
        print(f"\n  {len(set(said))}/{len(said)} of our messages distinct")

    print("\nFROM THE LOG (harness decisions, which only the log knows)")
    text, error = read_window("omegaclaw", "60m", tail=60000)
    if error:
        print(f"  unavailable: {error}")
    else:
        tally = collections.Counter()
        for (_verb, tag), n in count_results(text).items():
            tally[tag] += n
        total = sum(tally.values()) or 1
        for tag in ("OK", "PENDING", "SKIPPED", "FAILED"):
            print(f"  {tag:8} {tally[tag]:4}  {100 * tally[tag] // total:3}%")
        speak = count_results(text, "SPEAK")
        sent = sum(v for (_v, tag), v in speak.items()
                   if tag in ("OK", "PENDING"))
        tried = sum(speak.values()) or 1
        print(f"  speaks that reached the world: {pct(sent, tried)}")

    # Uptime sits next to the outcome numbers on purpose. Every deploy restarts
    # the agent, and a restart discards who has written to us, who we have met
    # and who we last wrote to - _warm_from_store recovers the durable facts and
    # not those. This file's own earlier measurement: the hour with no deploy in
    # it answered 9 of 11 threads, the two hours carrying five deploys managed 1
    # of 9 and 0 of 17.
    #
    # So a short uptime is context for a bad number, and printing them apart
    # invites reading the number as the city's fault.
    print("\nUPTIME  (a short one is context for the numbers above)")
    for name in ("omegaclaw", "vllm-mcity"):
        out = subprocess.run(
            ["docker", "inspect", name, "--format",
             "{{.State.StartedAt}}|{{.RestartCount}}"],
            capture_output=True, text=True, timeout=30)
        started, _, count = (out.stdout or "|").strip().partition("|")
        hours = ""
        try:
            # timegm, not mktime. StartedAt is UTC and mktime reads a struct as
            # LOCAL time, which printed "up -7.5h" on the first run - the same
            # class of mistake as the four-hour daemon clock this repo already
            # documents, and just as easy to read straight past.
            when = calendar.timegm(time.strptime(started[:19],
                                                 "%Y-%m-%dT%H:%M:%S"))
            hours = f"  up {(time.time() - when) / 3600:.1f}h"
        except (ValueError, OverflowError):
            pass
        print(f"  {name:12} restarts={count}{hours}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
