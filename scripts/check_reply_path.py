#!/usr/bin/env python3
"""Would the harness notice if somebody wrote to us right now?

This exists because the answer was NO for a full pass and nothing showed it. A
filter on threadStatus removed every candidate from the waiting list - every
thread this world returns is already closed - so waiting= read 0 in 885 of 885
samples and the agent answered nobody. The suite was green, because the tests
encoded the same wrong assumption as the code, and a silent harness in a quiet
city looks exactly like a working harness in a quiet city.

So this does not wait for the city. It takes a REAL inbound thread from the live
world, redates it to now, and asks the harness's own waiting logic whether that
person is owed a reply.

    python3 scripts/check_reply_path.py

Exit 1 when the harness would miss them. Run it after any change to the thread,
waiting or reachability code - which is most changes this repo makes.
"""
import json
import subprocess
import sys
import time

AGENT = "user-agent-ow0v8z9lg4v5kyr"


def live_threads():
    out = subprocess.run(
        ["docker", "exec", "omegaclaw", "sh", "-c",
         f"curl -s -m 20 'http://localhost:8080/mcity/api/agents/{AGENT}"
         f"/threads?limit=40'"],
        capture_output=True, text=True, timeout=90)
    try:
        return json.loads(out.stdout).get("threads") or []
    except Exception:
        return []


def main():
    sys.path.insert(0, "plugins/mcity")
    import mcity_client as mc                       # noqa: E402

    rows = live_threads()
    if not rows:
        print("no threads from the live world - is the agent connected?")
        return 1

    inbound = [t for t in rows if t.get("initiatorAgentId") != AGENT]
    if not inbound:
        print("no inbound thread in the last 40 to model; try again later")
        return 0

    now = int(time.time() * 1000)
    row = dict(inbound[0])
    who = row.get("initiatorAgentId")
    # 150 seconds, not 20. The world does not publish an inbound thread to us
    # until after it has closed - measured, one appeared at 146s old - so a
    # 20-second fixture models a state that never occurs, and this check passed
    # for several passes while every real inbound message was being written off
    # as too old. A check that models an impossible state is worse than none.
    row["threadLastMessageAtMs"] = now - 150000
    row["threadCreatedAtMs"] = now - 150000

    print(f"modelling a real inbound thread from {who}")
    print(f"  status={row.get('threadStatus')} "
          f"i={row.get('initiatorMessageCount')} "
          f"r={row.get('recipientMessageCount')}, redated to 20s ago")

    mc._c = lambda key, default=None: AGENT if key == "agent_id" else default
    closed = mc._thread_closed(row)
    counterpart = mc._thread_counterpart(row, AGENT)
    mine = mc._thread_mine(row, AGENT)

    print(f"  _thread_closed      -> {closed}   (True means we ignore them)")
    print(f"  _thread_counterpart -> {counterpart}")
    print(f"  _thread_mine        -> {mine}     ('no' means they are owed a reply)")

    ok = (not closed) and counterpart == who and mine == "no"
    if ok:
        print("\nthe harness would notice this person and offer to answer them")
        return 0
    print("\nthe harness would MISS this person. waiting= will stay at zero and "
          "the agent will answer nobody, which is indistinguishable from a quiet "
          "city until somebody checks.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
