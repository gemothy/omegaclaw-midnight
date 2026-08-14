#!/usr/bin/env python3
"""Grep the agent's log with the prompt stripped, against the right clock.

    python3 scripts/logs.py 20m 'do not disturb'      # count and show matches
    python3 scripts/logs.py 20m                       # just the window

Two traps this avoids, both of which have produced confident wrong answers here
more than once:

  the prompt        the mission text NAMES most of the tokens worth counting and
                    is echoed into the log every turn. `they-said=` once read 658
                    where the truth was 0, and a plain `grep -i error` returned
                    61KB of prompt because the word appears in the instructions
  the clock         the Docker daemon on this host runs about four hours behind
                    it, so `docker logs --since 20m` is not twenty minutes

dockerlogs.read_window handles both. This is the one-liner front end, so reaching
for `docker logs | grep` stops being the path of least resistance.
"""
import re
import sys

sys.path.insert(0, "scripts")
from dockerlogs import read_window                      # noqa: E402


def main():
    window = sys.argv[1] if len(sys.argv) > 1 else "20m"
    pattern = sys.argv[2] if len(sys.argv) > 2 else None

    text, error = read_window("omegaclaw", window)
    if error:
        print(f"cannot read the window honestly: {error}")
        return 1
    lines = (text or "").splitlines()
    if not pattern:
        print(f"{len(lines)} lines in the last {window} (prompt excluded)")
        return 0

    hits = [line for line in lines if re.search(pattern, line)]
    print(f"{len(hits)} matches for {pattern!r} in the last {window} "
          f"(prompt excluded)")
    for line in hits[:12]:
        print(f"  {line.strip()[:150]}")
    if len(hits) > 12:
        print(f"  ... and {len(hits) - 12} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
