#!/usr/bin/env python3
"""Can the operator reach the agent, and does the agent only speak when told to?

The operator channel is the one that reaches a real person's phone, so its
failures are the ones a human notices personally. Nothing here had ever checked
it.

Three questions, in the order that matters:

  is the bot reachable          getMe through the proxy that injects the token
  can a send actually land      a send goes nowhere until a chat id is known,
                                which happens when the operator first messages
                                the bot. Until then EVERY send silently succeeds
                                at nothing - which looks exactly like a working
                                guard and is not one
  does it speak unbidden        the mission allows send in exactly two
                                situations and forbids idle reports outright

That third one is why this exists. Measured over 180 minutes, the agent emitted:

    ((send "is forbidden on this turn. Only one world action.") None))

It was writing prose - "send is forbidden on this turn" - and a line is run as
whatever its first word names, so the sentence became a real send. It reached
nobody only because no chat id was configured. Eight further lines parsed as
bogus commands the same way.

    python3 scripts/check_operator_channel.py [window]

Exit 1 when a spurious send is found AND delivery is possible, because that
combination puts nonsense on somebody's phone. When delivery is not possible it
reports and passes - the risk is real but latent, and failing a check for a
message nobody received would cry wolf.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, "scripts")
from dockerlogs import read_window                        # noqa: E402

ENV_FILE = os.path.expanduser("~/.config/omegaclaw/mcity.env")


def bot_reachable():
    out = subprocess.run(
        ["docker", "exec", "omegaclaw", "sh", "-c",
         "curl -s -m 20 'http://localhost:8080/telegram/getMe'"],
        capture_output=True, text=True, timeout=60)
    text = out.stdout or ""
    name = re.search(r'"username"\s*:\s*"([^"]+)"', text)
    return ('"ok":true' in text.replace(" ", "").lower(),
            name.group(1) if name else None)


def can_deliver():
    """A chat id has to exist before any send reaches anybody."""
    try:
        with open(ENV_FILE, encoding="utf-8") as fh:
            keys = [ln.split("=", 1)[0].strip() for ln in fh
                    if "=" in ln and not ln.strip().startswith("#")]
    except OSError:
        return None
    return "TG_CHAT_ID" in keys


def main():
    window = sys.argv[1] if len(sys.argv) > 1 else "180m"

    ok, name = bot_reachable()
    print(f"  {'ok  ' if ok else 'FAIL'}  bot reachable"
          f"{f' ({name})' if name else ''}")

    deliverable = can_deliver()
    if deliverable is None:
        print("  ??    cannot read the env file to see if a chat id is set")
    elif deliverable:
        print("  ok    a chat id is configured, so a send reaches somebody")
    else:
        print("  --    no TG_CHAT_ID: every send currently reaches nobody. "
              "That is not a guard, it is an accident of setup")

    text, error = read_window("omegaclaw", window, tail=60000)
    if error:
        print(f"  note: {error}")
    log = text or ""

    sends = re.findall(r'\(\(send "([^"]{0,120})', log)
    prose = re.findall(r'\(\(No "([^"]{0,80})', log)
    print(f"\n  {len(sends)} send command(s) emitted in {window}")
    for s in sends[:5]:
        print(f"      {s[:96]}")
    print(f"  {len(prose)} line(s) of prose parsed as a bogus command")

    # A send whose text starts mid-sentence is prose that got executed. A real
    # one answers the operator or reports an outcome; it does not begin "is".
    spurious = [s for s in sends
                if re.match(r"(is|was|are|were|has|have|will|would|not)\b", s)]
    if spurious:
        print(f"\n  {len(spurious)} of those are prose, not a message anybody "
              f"meant to send")
        if deliverable:
            print("  and a chat id IS configured, so these land on a phone.")
            return 1
        print("  they reached nobody only because no chat id is configured.")
    if not ok:
        return 1
    print("\noperator channel is reachable and nothing spurious was delivered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
