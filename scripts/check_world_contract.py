#!/usr/bin/env python3
"""Check that the world still sends the fields this harness reads.

Written after a silent decay. `canStartConversation` was TRUE in our own payload
on 2026-08-12, which is the measurement that settled whether walking blocks a
conversation, and it was absent two days later. The harness fell back to a worse
rule, no test failed - both branches were covered - and 34 speaks a window were
refused for nothing. The tests knew both branches; what nobody knew was which
branch reality was taking.

So this asks the live world, not a fixture. Run it after any deploy, and whenever
behaviour changes for no reason visible in the diff:

    python3 scripts/check_world_contract.py

Exit status is 1 if a REQUIRED field is missing, so it can gate a deploy. Optional
fields only warn: some genuinely come and go with the agent's state, and the point
is to SEE that rather than to discover it three days later in a skip count.
"""
import json
import subprocess
import sys

AGENT = "user-agent-ow0v8z9lg4v5kyr"
BASE = "http://localhost:8080/mcity/api"

# field -> what stops working without it
REQUIRED = {
    "agent": {
        "position": "every distance and same-space judgement",
        "activeAction": "whether we are mid-action at all",
        "inventory": "holding=, the eat guard, and what we can afford",
        "hunger": "hunger=, the whole eating procedure",
    },
    "threads": {
        "threadLastMessageAtMs": "deferred delivery confirmation",
        "initiatorAgentId": "who opened it, and so who owes a reply",
        "recipientAgentId": "the counterpart, for every thread rule",
        "initiatorMessageCount": "whether they have spoken",
        "recipientMessageCount": "whether WE have replied",
        "threadStatus": "open or closed",
    },
    "roster": {
        "canSpeak": "the reachability verdict",
        "isOpenToTalk": "do-not-disturb",
        "isOnSameMap": "reachability",
        "isTalkingToYou": "not refusing to answer the person talking to us",
        "activeAction": "sleep and engage blocking a conversation",
        "name": "who=, so the agent has somebody to talk to",
        "profession": "who=",
    },
}

OPTIONAL = {
    "agent": {
        "canStartConversation": "our own engagement; the fallback is a guess "
                                "that has twice been wrong",
    },
    "roster": {
        "distance": "nearest-first ordering",
    },
}


def fetch(path):
    out = subprocess.run(
        ["docker", "exec", "omegaclaw", "sh", "-c",
         f"curl -s -m 20 '{BASE}/{path}'"],
        capture_output=True, text=True, timeout=60)
    try:
        return json.loads(out.stdout)
    except Exception:
        return None


def sample(payload, kind):
    """One representative dict for this payload kind, or None."""
    if not isinstance(payload, dict):
        return None
    if kind == "agent":
        return payload.get("agent") or payload
    items = payload.get("threads") or payload.get("agents") or []
    return items[0] if items and isinstance(items[0], dict) else None


def main():
    payloads = {
        "agent": (f"agents/{AGENT}", "agent"),
        "threads": (f"agents/{AGENT}/threads?limit=5", "threads"),
        "roster": (f"skill/agents/{AGENT}/agents", "roster"),
    }
    missing_required = []
    for name, (path, kind) in payloads.items():
        row = sample(fetch(path), kind)
        if row is None:
            print(f"{name:8} UNREADABLE - {path}")
            missing_required.append((name, "<whole payload>", "everything"))
            continue
        present = [f for f in REQUIRED.get(name, {}) if f in row]
        absent = [f for f in REQUIRED.get(name, {}) if f not in row]
        print(f"{name:8} {len(present)}/{len(REQUIRED.get(name, {}))} required "
              f"fields present")
        for field in absent:
            print(f"         MISSING  {field} -> {REQUIRED[name][field]}")
            missing_required.append((name, field, REQUIRED[name][field]))
        for field, why in OPTIONAL.get(name, {}).items():
            state = "present" if field in row else "ABSENT"
            print(f"         optional {field}: {state} -> {why}")

    if missing_required:
        print(f"\n{len(missing_required)} required field(s) gone. The harness "
              "reads these; behaviour degrades quietly without them.")
        return 1
    print("\nthe world still sends everything this harness depends on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
