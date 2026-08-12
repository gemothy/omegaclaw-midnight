"""Structural guards against one rule drifting into a second caller.

This is the defect this codebase produces most often, and every instance cost a
live failure that took a deploy cycle to find:

  * the reachability verdict was re-derived four times - in the rendered
    can-speak= column, in the candidate list, in the opener, and in talk-to -
    and each copy eventually recommended somebody the next check refused;
  * the thread counterpart was re-derived in four places and only the renderer
    knew this world has no participants list, so delivery confirmation matched
    no thread and 22 delivered messages were reported PENDING;
  * travel_district carried its own copy of the movement argument checks, so two
    guards added to _destination_action never applied to it and six world calls
    were spent being told the agent was already in the district it stood in.

Reviewing for this by eye has failed six times, so it is checked here instead.
Each rule names the single function that owns it. If a new caller genuinely
needs raw access, add it to the allowance WITH the reason - the point is that
the decision is deliberate and visible, not that it never happens.

Run:
    OMEGACLAW_SKIP_LIVE_CLEANUP=1 python3 -m pytest Autotests/test_single_source_of_truth.py -q
"""
import pathlib
import re

import pytest

CLIENT = pathlib.Path(__file__).resolve().parent.parent / "plugins" / "mcity" / "mcity_client.py"


def _functions(source):
    """Split the module into (name, body) pairs, top-level defs only."""
    parts = re.split(r"\ndef ([A-Za-z_][\w]*)\(", source)
    out = []
    for i in range(1, len(parts), 2):
        out.append((parts[i], parts[i + 1]))
    return out


def _callers_touching(pattern, allowed):
    hits = []
    for name, body in _functions(CLIENT.read_text()):
        if name in allowed:
            continue
        if re.search(pattern, body):
            hits.append(name)
    return hits


def test_reachability_is_decided_in_one_place():
    """Whether a message can land is _can_be_reached's decision.

    _entry_reachable computes it from a roster row and _note_can_speak stores it;
    everything else must ask rather than read the cache directly."""
    offenders = _callers_touching(
        r"_CAN_SPEAK\[",
        allowed={"_note_can_speak", "_can_be_reached", "reset_runtime_state"})
    assert not offenders, (
        f"{offenders} read the reachability cache directly. Four copies of this "
        "rule drifted apart before; call _can_be_reached instead.")


def test_the_thread_counterpart_is_derived_in_one_place():
    """This world names initiatorAgentId and recipientAgentId and ships no
    participants list. Every caller that guessed otherwise silently matched
    nothing."""
    offenders = _callers_touching(
        r'_get\(item, "participants"', allowed={"_thread_counterpart", "threads"})
    assert not offenders, (
        f"{offenders} look for a participants list this world does not send. "
        "Call _thread_counterpart, which knows every spelling.")


def test_every_movement_skill_shares_the_movement_guards():
    """move-area, travel-district and move-agent all take a destination id and
    all need the same checks: a valid id, not where we already stand, and not the
    district the world just said we are in."""
    source = CLIENT.read_text()
    # move_tile is deliberately excluded: it takes x y coordinates rather than a
    # destination id, so "you are already there" and "you are already in that
    # district" have nothing to compare against. The guards are about names.
    for skill in ("move_area", "travel_district", "move_agent"):
        match = re.search(rf"\ndef {skill}\(.*?\n(?=\n@|\ndef )", source, re.S)
        assert match, f"{skill} not found"
        body = match.group(0)
        assert "_destination_action" in body, (
            f"{skill} builds its own destination action. travel_district did "
            "that and missed two guards added to _destination_action.")
