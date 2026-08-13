#!/usr/bin/env python3
"""Which of this harness's mechanisms have actually fired lately?

Twice now a mechanism has been dead in production while passing every test:

  the exit door       gated on space_kind, which came only from the retired
                      context skill - zero requests to that endpoint, so indoors
                      was always False and the door was never offered. Three
                      passes.
  the empty-room guard gated on the area-kind table, fed only by the retired
                      areas skill - so the agent walked into a room with one
                      occupant 28 times while the guard fired twice.

Both passed their unit tests, because the tests populate the state by hand. What
neither had was any check that the path RUNS against the live world.

This lists the observable mechanisms and whether each has left a trace in the
log. Silence is not proof of death - a guard with nothing to guard against is
quiet for good reasons - so read this as a list of things to ASK about, not a
verdict. The ones that have never fired across several windows are the suspects.

    python3 scripts/check_mechanisms.py [window]

READ THE COUNTS AS BOOLEANS, NOT RATES. Cross-checked against a second log
reader over the same window: some agree exactly (9 vs 9, 8 vs 8, 41 vs 39) and
some do not (who= 196 vs 155, cold_opens_paused 16 vs 0). Window boundaries
account for the small gaps; the large one is unexplained. Whether a mechanism
fired at all is the question this tool answers well, and it is the question that
found both dead ones.
"""
import re
import subprocess
import sys

# name -> (regex over the logs, why it might legitimately be silent)
MECHANISMS = {
    "vitals: who= (name and trade)":
        (r"who=\S+", "nobody reachable to name"),
    "vitals: met-before= (prior encounter)":
        (r"met-before=", "everybody nearby is new"),
    "vitals: waiting= names somebody":
        (r"\(answer ", "nobody has written to us"),
    "vitals: they-said= (their words)":
        (r"they-said=", "nobody has written to us"),
    "vitals: route to people":
        (r"exactly: \(mcity-(?:travel-district|move-area)", "people are here"),
    "vitals: exit door offered":
        (r"exactly: \(mcity-exit-building\)", "not indoors, or people are here"),
    "vitals: way to food":
        (r"vitals[^\n]*\(mcity-trade", "not hungry"),
    "guard: empty room refused":
        (r"leaves_the_people", "never tried to walk into one"),
    "guard: cold opens throttled":
        (r"cold_opens_paused", "openers are landing"),
    "guard: already refused by the world":
        (r"reason=unreachable", "everybody is accepting"),
    "guard: read cooldown":
        (r"just_read", "not repeating reads"),
    "memory: delivery confirmed late":
        (r"silent-for=\d+m", "nothing delivered yet"),
    "world: speak accepted":
        (r"MCITY-SPEAK-(?:OK|PENDING)", "nothing sent"),
    "world: thread answered by us":
        (r"MCITY-SPEAK-OK", "no confirmed delivery"),
    "diagnostic: route declined":
        (r"mcity route: none", "the route always had an answer"),
}


def main():
    window = sys.argv[1] if len(sys.argv) > 1 else "25m"
    out = subprocess.run(["docker", "logs", "--since", window, "omegaclaw"],
                         capture_output=True, text=True, timeout=180)
    text = (out.stdout or "") + (out.stderr or "")
    if not text:
        print("no logs")
        return 1

    # Drop the prompt lines before counting anything.
    #
    # The mission text NAMES most of these tokens - "the vitals line carries who=
    # with that person's NAME", "carries they-said= with their own words" - and it
    # is echoed in every prompt. Counting them straight had they-said= firing 409
    # times while waiting= was silent, which is impossible: they are appended in
    # the same statement. The first number was my own instructions, read back to
    # me. That mistake has been made in this project before and it is exactly the
    # kind a tool like this is supposed to prevent.
    # Dropping lines that carry PROMPT: is not enough - the raw docker log wraps
    # the prompt across many lines, so most of the mission text survived and
    # they-said= "fired" 10 times in a window where the real count was 0. The
    # prompt is also the only thing here longer than a couple of thousand
    # characters, and the only thing carrying the MIDNIGHT_CITY banner.
    def _is_prompt(line):
        return ("CHARS_SENT:" in line or "PROMPT:" in line
                or "MIDNIGHT_CITY" in line or "SKILLS:" in line
                or len(line) > 2000)

    text = "\n".join(line for line in text.splitlines() if not _is_prompt(line))

    fired, silent = [], []
    for name, (pattern, excuse) in MECHANISMS.items():
        hits = len(re.findall(pattern, text))
        (fired if hits else silent).append((name, hits, excuse))

    print(f"in the last {window}:\n")
    for name, hits, _ in sorted(fired, key=lambda r: -r[1]):
        print(f"  fired {hits:>5}  {name}")
    print()
    for name, _, excuse in silent:
        print(f"  SILENT        {name}")
        print(f"                could be legitimate: {excuse}")
    print(f"\n{len(fired)} of {len(MECHANISMS)} mechanisms left a trace. A "
          "mechanism silent across SEVERAL windows is worth opening the code "
          "for - both dead ones found so far looked exactly like this.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
