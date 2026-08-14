#!/usr/bin/env python3
"""Run every live check, in the order that stops false conclusions.

Four checks accumulated over this session, each because something was silently
dead and I found it by accident. Keeping them as separate scripts means running
them from memory, and forgetting one is exactly how the last three defects
survived - a mechanism nobody looked at, a guard gated on a read nobody made, a
window nobody measured.

Order matters:

  1 can it act            silence from a dead agent means nothing at all, so this
                          gates everything below it
  2 world contract        the fields we read must still be sent
  3 reply path            would a real inbound message be noticed
  4 escape                would a room that refuses us offer a way out
  5 mechanisms            which of the harness's parts have left a trace

    python3 scripts/check_all.py [window]

Exit 1 if any check fails. The mechanism audit never fails on its own - silence
there is a question, not a verdict - so it prints and is read by a human.
"""
import subprocess
import sys

CHECKS = [
    # FIRST, and gating. Three passes of fixes were measured against a container
    # running a three-hour-old image, because the launcher never built. Nothing
    # below this can be interpreted while the wrong code is running.
    ("running this working tree", "check_deployed.py", True),
    ("can it act", "can_it_act.py", True),
    ("world contract", "check_world_contract.py", True),
    ("reply path", "check_reply_path.py", True),
    ("escape from a refusing room", "check_escape.py", True),
    # What we SAID, not just whether it arrived. These messages reach other
    # people's agents under a real person's name, so a leaked refusal reason or
    # another player's do-not-disturb status is said in public.
    ("nothing internal said out loud", "check_outbound_quality.py", False),
    # The channel that reaches a real person's phone.
    ("operator channel", "check_operator_channel.py", False),
]


def run(script, args):
    proc = subprocess.run([sys.executable, f"scripts/{script}", *args],
                          capture_output=True, text=True, timeout=400)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    window = sys.argv[1] if len(sys.argv) > 1 else "20m"
    failed = []

    for name, script, gating in CHECKS:
        args = [window] if script == "can_it_act.py" else []
        try:
            code, out = run(script, args)
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT  {name}")
            failed.append(name)
            continue
        verdict = "ok  " if code == 0 else "FAIL"
        last = [line for line in out.strip().splitlines() if line.strip()]
        print(f"  {verdict}  {name}")
        if code != 0:
            failed.append(name)
            for line in last[-3:]:
                print(f"          {line.strip()[:100]}")
        if gating and code != 0 and script in ("can_it_act.py",
                                                "check_deployed.py"):
            why = ("the agent cannot act" if script == "can_it_act.py"
                   else "the container is running different code")
            print(f"\nStopping: nothing below this can be interpreted while "
                  f"{why}.")
            return 1

    print("\n-- mechanisms (silence is a question, not a verdict) --")
    try:
        _, out = run("check_mechanisms.py", [window])
        for line in out.strip().splitlines():
            if line.strip().startswith(("fired", "SILENT")) or " of " in line:
                print(f"  {line.strip()[:96]}")
    except subprocess.TimeoutExpired:
        print("  TIMEOUT")

    if failed:
        print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
        return 1
    print("\nevery live check passes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
