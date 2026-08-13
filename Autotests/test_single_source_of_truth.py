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


def test_the_worlds_refusals_are_recorded_in_exactly_one_place():
    """Asleep, gone, closed and unreachable-destination used to be four dicts,
    each with its own TTL, its own prune and its own reset line. They are one
    idea, and every duplicated rule in this file has eventually drifted from its
    copy - which is what the rest of this module exists to catch."""
    offenders = _callers_touching(
        r"_REFUSED\[[^\]]+\] =",
        allowed={"_remember_refusal", "reset_runtime_state"})
    assert not offenders, (
        f"{offenders} write the refusal memory directly. Call "
        "_remember_refusal, which owns the timestamp and the pruning.")


def test_no_refusal_kind_exists_without_a_ttl():
    """A kind with no entry in the table raises KeyError inside the read path,
    which is the pruning silently never running - the failure the four separate
    dicts actually had."""
    source = CLIENT.read_text()
    table = re.search(r"_REFUSAL_TTL_MS = \{(.*?)\n\}", source, re.S)
    assert table, "_REFUSAL_TTL_MS not found"
    declared = set(re.findall(r'"(\w+)":', table.group(1)))
    used = set(re.findall(r'_(?:remember_refusal|refused_ago_ms|refused_keys)\(\s*"(\w+)"',
                          source))
    assert used <= declared, f"{used - declared} have no TTL; they never expire"


def test_a_refusal_that_names_a_next_move_is_never_a_failure():
    """The core system prompt tells the agent "if you see command errors, fix the
    format and re-invoke". So FAILED means retry, and a refusal we have attached a
    do-THIS to is precisely one that must NOT be retried - we have just named
    something else to do. no_food shipped as FAILED and was caught here.

    This is the bug that once made 105 of 106 commands a single refused call, each
    refusal instructing the agent to make it again."""
    source = CLIENT.read_text()
    skips = re.search(r"_SKIP_REASONS = frozenset\(\((.*?)\)\)", source, re.S)
    assert skips, "_SKIP_REASONS not found"
    declared = set(re.findall(r'"(\w+)"', skips.group(1)))
    promoted = set(re.findall(
        r'_promote_command\(\s*\n?\s*_failed\(\s*\n?\s*"[A-Z-]+",\s*"(\w+)"', source))
    missing = promoted - declared
    assert not missing, (
        f"{sorted(missing)} name a next move but are tagged FAILED, which reads "
        "as 'retry this'. Add them to _SKIP_REASONS.")


def test_the_vitals_line_never_offers_two_people_or_two_commands():
    """The agent is told a parenthesised command IS its next move and that
    answering outranks everything. Both instructions are void the moment the line
    beneath them names a second target, and this has now happened twice: the food
    route against talk-to, then waiting against talk-to - 24 of 63 lines, over a
    half hour in which four agents opened threads with us and we answered none."""
    source = CLIENT.read_text()
    body = source[source.index("def _vitals_line"):]
    body = body[:body.index("\ndef _out")]
    assert "elif not waiting:" in body, (
        "talk-to must yield to somebody who is owed a reply")
    assert "and not food_route" in body, (
        "the route must yield to a hungry agent's way to food")


def test_only_one_rule_decides_that_an_action_blocks_talking():
    """Asked about our own action and about everybody else's, and written twice
    before: any-action-counts put 134 speaks out of reach in one window and 34 in
    another, while the world said "speaker is in do not disturb" zero times."""
    # the tuple's own declaration is not a caller of it
    offenders = _callers_touching(
        r"_ACTIONS_THAT_BLOCK_TALK(?! = \()", allowed={"_action_blocks_talk"})
    assert not offenders, (
        f"{offenders} decide this for themselves. Call _action_blocks_talk.")


def test_the_cold_open_throttle_can_be_cleared_as_well_as_set():
    """Both halves must exist. The accepted-opener path was written into a commit
    whose edit silently failed to apply, so nothing ever cleared the streak and
    the throttle could only tighten - shipped, and described in the commit message
    as working."""
    source = CLIENT.read_text()
    assert "_note_cold_open(refused=False)" in source, (
        "nothing clears the streak; the throttle only tightens")
    assert "_note_cold_open(refused=True)" in source
    assert "_note_cold_open_sent()" in source


def test_both_waiting_lists_exclude_a_closed_thread():
    """waiting= is built twice - once by the vitals refresh and once while
    rendering mcity-threads - and only the refresh learned that a closed thread is
    nobody waiting. The rendered one kept sending the agent to answer
    conversations the world had already shut."""
    source = CLIENT.read_text()
    assert source.count("_thread_closed(item)") >= 2, (
        "both waiting lists must apply it")
