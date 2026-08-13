#!/usr/bin/env python3
"""Would the agent be told how to leave a room that will not talk to it?

Four passes running, the agent has been trapped indoors in a room where every
send is refused, and each fix was followed by a deploy whose restart cleared the
trap before the mechanism could show itself. The chain has three links and each
one was broken separately:

  the count       reachable ignored refusals, so it sat at 27-30 in a room where
                  all of them refused, and the door was gated on it reaching 0
  the condition   the route offered the door only when the room was EMPTY, not
                  when it refused
  the caller      _vitals_line only asked for a route when reachable was 0, so
                  the fixed condition was never consulted

So stop waiting for the world. This puts the harness in the trapped state - real
space kind from the live world, a full room, and the cold-open throttle engaged -
and asks whether it produces the door.

    python3 scripts/check_escape.py

Exit 1 when it would not. Run it after any change to the route, the reachability
count or the throttle.
"""
import json
import subprocess
import sys

AGENT = "user-agent-ow0v8z9lg4v5kyr"


def live_navigation():
    out = subprocess.run(
        ["docker", "exec", "omegaclaw", "sh", "-c",
         f"curl -s -m 20 'http://localhost:8080/mcity/api/skill/agents/{AGENT}"
         f"/navigation-options'"],
        capture_output=True, text=True, timeout=90)
    try:
        return json.loads(out.stdout)
    except Exception:
        return {}


def main():
    sys.path.insert(0, "plugins/mcity")
    import mcity_client as mc                       # noqa: E402

    nav = live_navigation()
    space = (nav.get("currentSpace") or {})
    exit_block = nav.get("exitBuilding") or {}
    print(f"live world says: space={space.get('id')} kind={space.get('kind')} "
          f"exit={exit_block.get('kind')} -> {exit_block.get('targetSpaceId')}")

    mc.reset_runtime_state()
    # The trapped state, with the world's own interior kind where we have one.
    mc._VITALS.update({
        "at_ms": mc._now_ms(),
        "space": "hacker-house-interior",
        "space_kind": "interior",
        "hunger": "normal(20)",
        "items": "crystal=5",
    })
    # A room that LOOKS full - this is the number that never fell.
    mc._REACHABLE.update({"n": 29, "at_ms": mc._now_ms()})
    for _ in range(mc._COLD_OPEN_STREAK):
        mc._note_cold_open(refused=True)

    throttled = mc._cold_opens_paused()
    hint = mc._travel_to_people_command() or ""
    line = mc._vitals_line() or ""

    print(f"  throttle engaged      : {bool(throttled)}")
    print(f"  route offers the door : {'(mcity-exit-building)' in hint}")
    print(f"  vitals carries it     : {'(mcity-exit-building)' in line}")

    if "(mcity-exit-building)" in line:
        print("\nthe agent would be told how to get out")
        return 0
    print("\nthe agent would sit there. The room looks full, every send is "
          "refused, and nothing on the line tells it to leave - which is the "
          "trap it has fallen into four times.")
    if hint and "(mcity-exit-building)" not in hint:
        print(f"  the route said instead: {hint[:110]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
