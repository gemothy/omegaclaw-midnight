#!/usr/bin/env python3
"""What did we actually SAY to real people, and was any of it harness internals?

Everything else here measures whether a message was delivered. Nothing checked
what was in it, and the failure this guards is not subtle: these messages reach
other people's agents in a shared world, so leaking a refusal reason or another
player's do-not-disturb status is a visible thing said in public under a real
person's name.

It reads the world's own record - latestMessagePreview on the threads WE opened -
not the log. That distinction is the reason this script exists. Sampling the log
for what looked like sent text turned up, in one 90-minute window:

    "Nova is in do not disturb, so I'm looking at you. What are you up to ...?"
    "FAILED with reason=action_failed, detail says target is in do not disturb"
    "then your sentence_quote_"

and none of them had been sent. They were drafts the harness refused, and the
model narrating its own results back into the log. The world's record for the
same window held 46 messages, 46 of them distinct and every one clean. A check
built on the log would have reported three serious leaks that never happened.

    python3 scripts/check_outbound_quality.py

Exit 1 if any harness internal reached a real person. Templating is reported but
never fails: "a line that would fit any stranger" is a judgement, and the mission
already carries that rule.
"""
import collections
import re
import sys

sys.path.insert(0, "scripts")
from reply_funnel import live_threads, AGENT              # noqa: E402

# Each of these has a specific way it could get into a message: a result line
# echoed back, an argument left unstripped, or the agent narrating harness state
# to somebody who has no idea what it means.
LEAKS = (
    ("another player's do-not-disturb", r"do.not.disturb"),
    ("a refusal reason", r"\breason=|\bFAILED\b|\bSKIPPED\b"),
    ("a raw agent id", r"user-agent-[\w-]+"),
    ("MeTTa escaping", r"_quote_|_apostrophe_|_newline_"),
    ("a harness suggestion", r"try-instead|do-THIS|do-NOT-repeat"),
    ("a result tag", r"MCITY-[A-Z]"),
    ("the untrusted marker", r"MC_UNTRUSTED"),
)


def main():
    rows = live_threads(120)
    if not rows:
        print("no threads from the live world - is the agent connected?")
        return 1
    ours = [t for t in rows if t.get("initiatorAgentId") == AGENT]
    said = [(t.get("latestMessagePreview") or "").strip() for t in ours]
    said = [s for s in said if s]
    if not said:
        print("we have said nothing the world still holds")
        return 0

    bad = []
    for name, pattern in LEAKS:
        for text in said:
            if re.search(pattern, text, re.I):
                bad.append((name, text))

    print(f"{len(said)} messages the world recorded us sending\n")
    for name, _ in LEAKS:
        n = sum(1 for b, _ in bad if b == name)
        print(f"  {'LEAK' if n else 'ok  '}  {name}: {n}")

    distinct = len(set(said))
    print(f"\n  {distinct}/{len(said)} distinct "
          f"({int(100 * distinct / len(said))}%)")
    repeats = [(t, n) for t, n in collections.Counter(said).most_common(3)
               if n > 1]
    for text, n in repeats:
        print(f"    said {n}x: {text[:88]}")
    if not repeats:
        print("    no message sent twice")

    if bad:
        print("\nHARNESS INTERNALS REACHED A REAL PERSON:")
        for name, text in bad[:5]:
            print(f"  {name}: {text[:100]}")
        return 1
    print("\nnothing we said carried harness internals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
