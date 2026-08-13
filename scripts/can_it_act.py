#!/usr/bin/env python3
"""Can the agent act at all? Ask this BEFORE reading anything it did.

Written after a whole pass was spent A/B-testing prompt wording against a harness
that had lost its control lease fifty minutes earlier. Every measurement in that
pass was of a corpse - three hypotheses "refuted" identically, because the agent
could not speak whatever the prompt said. The tell was in plain sight: speak
failures had jumped an order of magnitude, and I read the conversation metrics
instead of the failure counts.

So this is the preamble. Silence from a healthy agent means something about the
city; silence from a dead one means nothing at all, and the difference is not
visible in any of the numbers I usually quote.

    python3 scripts/can_it_act.py [window]

Exit 1 when the agent cannot act, so a measurement script can refuse to draw
conclusions.
"""
import collections
import re
import time
import subprocess
import sys

DEAD_REASONS = ("lease_lost", "lease_expired", "not_ready", "no_lease",
                "read_only", "disabled")


def logs(window):
    out = subprocess.run(["docker", "logs", "--since", window, "omegaclaw"],
                         capture_output=True, text=True, timeout=180)
    return (out.stdout or "") + (out.stderr or "")


def main():
    window = sys.argv[1] if len(sys.argv) > 1 else "15m"
    text = logs(window)
    if not text:
        print("no logs - is the container running?")
        return 1

    verdicts = []

    iterations = len(re.findall(r"---------iteration", text))
    verdicts.append(("thinking", iterations > 0,
                     f"{iterations} decisions in {window}"))

    # the lease, which is the failure that started this
    dead = collections.Counter(
        r for r in re.findall(r"MCITY-[A-Z-]+-(?:FAILED|SKIPPED) reason=(\w+)", text)
        if r in DEAD_REASONS)
    verdicts.append(("has control", not dead,
                     "clear" if not dead else f"{dict(dead)}"))

    # did the world accept anything at all
    ok = len(re.findall(r"MCITY-[A-Z-]+-(?:OK|PENDING)", text))
    verdicts.append(("world accepts actions", ok > 0,
                     f"{ok} accepted"))

    # is the model answering
    gateway = len(re.findall(r"504 Gateway", text))
    verdicts.append(("model answering", gateway == 0,
                     "clear" if not gateway else f"{gateway} x 504"))

    # is the store holding
    degraded = len(re.findall(r"store=degraded", text))
    verdicts.append(("store healthy", degraded == 0,
                     "clear" if not degraded else f"{degraded} degraded results"))

    # When did anybody last write to us? Without this, waiting=0 reads as a
    # broken reply path, and I have twice started debugging one that was working:
    # the freshest inbound thread was 1196 seconds old while I was staring at
    # "waiting non-zero: 0 of 817" over a thirty minute window. Nothing had
    # arrived. A quiet city and a deaf agent look identical from the log alone.
    try:
        import json as _json
        agent = "user-agent-ow0v8z9lg4v5kyr"
        got = subprocess.run(
            ["docker", "exec", "omegaclaw", "sh", "-c",
             f"curl -s -m 20 'http://localhost:8080/mcity/api/agents/{agent}"
             f"/threads?limit=40'"],
            capture_output=True, text=True, timeout=90)
        rows = _json.loads(got.stdout).get("threads") or []
        inbound = [r for r in rows if r.get("initiatorAgentId") != agent]
        now = time.time() * 1000
        ages = sorted(int((now - (r.get("threadLastMessageAtMs") or 0)) / 1000)
                      for r in inbound)
        if ages:
            print(f"  --    last inbound message   {ages[0]}s ago "
                  f"({len([a for a in ages if a < 900])} in the last 15 min)")
        else:
            print("  --    last inbound message   none in the visible history")
    except Exception:      # noqa: BLE001
        print("  --    last inbound message   could not read the world")

    ok_all = True
    for name, good, detail in verdicts:
        print(f"  {'OK ' if good else 'DEAD'}  {name:22} {detail}")
        ok_all = ok_all and good

    if not ok_all:
        print("\nthe agent cannot fully act. Nothing it did or did not do this "
              "window is evidence about the city, the prompt or the model.")
        return 1
    print("\nthe agent can act; its behaviour this window means something")
    return 0


if __name__ == "__main__":
    sys.exit(main())
