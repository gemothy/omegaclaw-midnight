#!/usr/bin/env python3
"""Is the code running in the container the code in this working tree?

Three consecutive passes of harness fixes were committed, "deployed" and then
verified against production, while the container kept running a three-hour-old
image. `omegaclaw-midnight-up` only ever ran `docker run` against a prebuilt tag
- it never built - so every deploy was a restart of the same old code.

What made it expensive was the verification. bad_args falling to zero was read as
the name-repair working. The repair was not in the image. A stale deploy does not
look like a failure; it looks like a fix that did not help, which is the most
misleading thing a change can look like, and the next pass then reasons from it.

    python3 scripts/check_deployed.py

Exit 1 when the container is running something other than this tree. Cheap, and
it belongs before every behavioural measurement, which is why check_all runs it
first - nothing below it can be interpreted while the wrong code is running.
"""
import hashlib
import pathlib
import subprocess
import sys

CONTAINER = "omegaclaw"
CORE = "/PeTTa/repos/OmegaClaw-Core"
# The files this fork actually changes. A mismatch in any of them means the
# measurement about to be taken is of something other than the working tree.
FILES = [
    "plugins/mcity/mcity_client.py",
    "plugins/mcity/mcity.metta",
]


def in_container(path):
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "cat", f"{CORE}/{path}"],
        capture_output=True, timeout=60)
    if out.returncode != 0:
        return None
    return out.stdout


def main():
    root = pathlib.Path(__file__).resolve().parent.parent
    bad = []
    for name in FILES:
        local = root / name
        if not local.exists():
            print(f"  ??  {name} is not in the working tree")
            continue
        running = in_container(name)
        if running is None:
            print(f"  FAIL  cannot read {name} from the container - is it up?")
            return 1
        here = hashlib.sha256(local.read_bytes()).hexdigest()[:12]
        there = hashlib.sha256(running).hexdigest()[:12]
        if here == there:
            print(f"  ok    {name}  ({here})")
        else:
            bad.append(name)
            print(f"  FAIL  {name}  tree={here} container={there}")

    if bad:
        print("\nThe container is NOT running this working tree. Anything you "
              "measure now is about other code.\n"
              "Rebuild and restart:  ~/bin/omegaclaw-midnight-up")
        return 1
    print("\nthe container is running this working tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
