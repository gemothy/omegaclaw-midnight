#!/usr/bin/env python3
"""Of the people who wrote to us and got no answer, WHERE did the reply die?

conversation_trend.py gives the rate - "we answer 31% of people who write to us"
- and that number has been read three different ways in this session, each
implying different work:

  the harness never surfaced them        a reply-path defect, ours to fix
  surfaced, but the world refused        the do-not-disturb ceiling, not ours
  surfaced, refused nothing, no attempt  the model declining to answer

Those are not distinguishable from a percentage, and guessing wrong costs a pass.
The reply eval says the model answers 40 out of 40 when told somebody is waiting,
so "the model will not answer" is the least likely of the three and was still the
first explanation reached for.

This walks each inbound thread and asks the log what happened to that specific
agent id.

    python3 scripts/reply_funnel.py [window]

Reads only. Counts are LOW when the window is truncated - it says so when that
happens, because a missing log line and an absent event look identical here.
"""
import collections
import json
import re
import subprocess
import sys
import time

sys.path.insert(0, "scripts")
from dockerlogs import read_window                      # noqa: E402

AGENT = "user-agent-ow0v8z9lg4v5kyr"


def live_threads(limit=60):
    out = subprocess.run(
        ["docker", "exec", "omegaclaw", "sh", "-c",
         f"curl -s -m 20 'http://localhost:8080/mcity/api/agents/{AGENT}"
         f"/threads?limit={limit}'"],
        capture_output=True, text=True, timeout=90)
    try:
        return json.loads(out.stdout).get("threads") or []
    except Exception:                                   # noqa: BLE001
        return []


def main():
    window = sys.argv[1] if len(sys.argv) > 1 else "60m"
    minutes = int(re.sub(r"[^0-9]", "", window) or 60)
    if window.strip().endswith("h"):
        minutes *= 60

    text, error = read_window("omegaclaw", window)
    if error:
        # A truncated window still answers the question for the threads it does
        # cover, so this warns rather than refuses - but it must warn, because
        # "no line for this agent" is exactly what a dropped line looks like.
        print(f"note: {error}\n")
    log = text or ""

    rows = live_threads()
    if not rows:
        print("no threads from the live world - is the agent connected?")
        return 1

    cutoff = int(time.time() * 1000) - minutes * 60000
    inbound = [t for t in rows
               if t.get("initiatorAgentId") != AGENT
               and (t.get("threadCreatedAtMs") or 0) >= cutoff]
    if not inbound:
        print(f"nobody wrote to us in the last {window}")
        return 0

    verdicts = collections.Counter()
    detail = collections.Counter()

    for row in inbound:
        who = row.get("initiatorAgentId") or ""
        if (row.get("recipientMessageCount") or 0) > 0:
            verdicts["answered"] += 1
            continue
        # Everything the log has to say about this specific person - the WHOLE
        # line, not the 120 characters after the id.
        #
        # The first version captured only what followed the id, so a failure
        # reported before it was invisible. It filed a reply that the world had
        # refused with do-not-disturb under "surfaced, never attempted", which
        # pointed the next pass at the model and the prompt when the send had in
        # fact gone out and been turned away. An instrument that mis-attributes
        # is worse than no instrument: this one decides which half of the system
        # gets worked on.
        mentions = [ln for ln in log.splitlines() if who in ln]
        blob = "\n".join(mentions)
        surfaced = bool(re.search(r"waiting=\d+ \(answer " + re.escape(who), log))
        failed = re.search(r"SPEAK-FAILED reason=(\w+)[^\n]{0,60}", blob)
        skipped = re.search(r"SPEAK-SKIPPED reason=(\w+)", blob)
        # The agent narrates its own results back into the log in prose - "`mcity
        # -speak` to <id> FAILED because target is in do-not-disturb mode" - and
        # that is sometimes the only trace, because the tagged line and the id do
        # not always share a line. Prose is weaker evidence than a tag, so it is
        # only consulted when no tag was found.
        if not failed and not skipped:
            prose = re.search(r"FAILED[^\n]{0,40}?(do.not.disturb|not found|"
                              r"recently closed)", blob, re.I)
            if prose:
                failed = prose

        if failed:
            verdicts["the world refused our reply"] += 1
            reason = failed.group(1) if failed.re.groups else "action_failed"
            # From the FAILING line only. Taken from the whole blob it picked up
            # the <<MC_UNTRUSTED ...>> preview of what the person had said to us
            # and printed that as the reason the reply failed, which reads as an
            # explanation and is just their message.
            line = next((ln for ln in mentions
                         if "FAILED" in ln or "failed" in ln), "")
            world = re.search(r"detail=<<MC_UNTRUSTED ([^<]{0,48})", line) \
                or re.search(r"(do.not.disturb|not found|recently closed)",
                             line, re.I)
            detail[f"{reason}: {(world.group(1) if world else '?').strip()}"] += 1
        elif skipped:
            verdicts["the harness skipped the reply"] += 1
            detail[f"skip {skipped.group(1)}"] += 1
        elif surfaced:
            verdicts["surfaced, never attempted"] += 1
        elif mentions:
            verdicts["seen in the log, never surfaced as waiting"] += 1
        else:
            verdicts["absent from the log entirely"] += 1

    # The world's own count first, because it cannot be truncated. Every verdict
    # below is inferred from log lines, and read_window drops the oldest when the
    # tail fills - so "absent from the log entirely" can mean the line was
    # dropped rather than never written. This number does not depend on that.
    allin = [t for t in rows if t.get("initiatorAgentId") != AGENT]
    for label, mins in (("this window", minutes), ("last 2h", 120), ("all held", 10 ** 6)):
        cut = int(time.time() * 1000) - mins * 60000
        sel = [t for t in allin if (t.get("threadCreatedAtMs") or 0) >= cut]
        if not sel:
            continue
        ans = sum(1 for t in sel if (t.get("recipientMessageCount") or 0) > 0)
        print(f"  world says: {ans}/{len(sel)} inbound answered "
              f"({int(100 * ans / len(sel))}%)  [{label}]")
    print()

    total = sum(verdicts.values())
    print(f"{total} people wrote to us in the last {window}\n")
    for name, n in verdicts.most_common():
        print(f"  {n:3}  {int(100 * n / total):3}%  {name}")
    if detail:
        print("\n  why, where the world or the harness said:")
        for name, n in detail.most_common(8):
            print(f"    {n:3}  {name[:78]}")

    ours = (verdicts["seen in the log, never surfaced as waiting"]
            + verdicts["absent from the log entirely"]
            + verdicts["surfaced, never attempted"])
    print(f"\n{ours} of {total} died on OUR side of the line" if ours else
          f"\nnone of the {total} died on our side of the line")
    return 0


if __name__ == "__main__":
    sys.exit(main())
