"""Offline unit tests for plugins/mcity/mcity_projection.py.

No container, no network, no token: the projection layer is pure stdlib and
storage-independent, so everything here runs in-process against test doubles.
The module is imported top-level (`import mcity_projection`) exactly the way
src/plugin.py loads plugin modules, and the way tests/mcity/test_mcity_client.py
imports its sibling.

What must hold, per docs/ARCHITECTURE-memory.md:

  * pinned candidates survive any budget pressure (rule 1),
  * shares follow declared weights, filled by descending score (rule 2),
  * an under-using source releases its remainder to the others (rule 3),
  * every drop is reported as an accurate count line, never silently (rule 4),
  * the scored fill never takes the output past budget_chars,
  * a 284-item roster under a small budget still shows the top-scored items
    AND an accurate dropped-count footer - the original production defect.

Run:
    pytest Autotests/test_mcity_projection.py -q
"""
import dataclasses
import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_PLUGIN = os.path.join(_REPO, "plugins", "mcity")
if _PLUGIN not in sys.path:
    sys.path.insert(0, _PLUGIN)

import mcity_projection as mp  # noqa: E402


# --------------------------------------------------------------------------
# test doubles and helpers
# --------------------------------------------------------------------------

FOOTER_RE = re.compile(r"^\[(?P<name>[^:]+): (?P<shown>\d+) of (?P<total>\d+) "
                       r"shown(?:, .*)?\]$")


class Source:
    """Minimal ContextSource double; weight/summary only when asked for."""

    def __init__(self, name, candidates, weight=None, summary=None):
        self.name = name
        self._candidates = list(candidates)
        if weight is not None:
            self.weight = weight
        if summary is not None:
            self.summary_for_dropped = summary

    def candidates(self, turn):
        return list(self._candidates)


class RosterSource:
    """The 284-agent roster shaped the way the integration will feed it:
    rows arrive through turn.world, scores rank unspoken-then-nearest."""

    name = "roster"
    weight = 1.0

    def candidates(self, turn):
        rows = turn.world["roster_rows"]
        total = len(rows)
        return [mp.Candidate(text=text, score=(total - index) / total,
                             source="roster")
                for index, text in enumerate(rows)]

    def summary_for_dropped(self, shown, total):
        return (f"[roster: {shown} of {total} shown, "
                f"ranked by unspoken-then-nearest]")


def cand(text, score, source="s", pinned=False):
    return mp.Candidate(text=text, score=score, source=source, pinned=pinned)


def turn(**world):
    return mp.TurnState(now_ms=1_754_700_000_000, human_message=None,
                        world=dict(world))


def footers(rendered):
    """{source name: (shown, total)} for every rule-4 line in the output."""
    found = {}
    for line in rendered.splitlines():
        match = FOOTER_RE.match(line)
        if match:
            found[match.group("name")] = (int(match.group("shown")),
                                          int(match.group("total")))
    return found


def body_lines(rendered, prefix):
    return [line for line in rendered.splitlines() if line.startswith(prefix)]


# --------------------------------------------------------------------------
# interfaces
# --------------------------------------------------------------------------

def test_candidate_is_frozen_with_defaults():
    candidate = cand("hello", 0.5)
    assert candidate.pinned is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.text = "mutated"


def test_turnstate_is_frozen_with_independent_world_dicts():
    state = mp.TurnState(now_ms=123)
    assert state.human_message is None
    assert state.world == {}
    state.world["k"] = "v"  # the dict itself is free-form and mutable
    assert mp.TurnState(now_ms=456).world == {}, "default_factory must not share"
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.now_ms = 789


def test_turnstate_is_passed_through_to_sources():
    seen = {}

    class Probe:
        name = "probe"

        def candidates(self, received):
            seen["turn"] = received
            return [mp.Candidate(text="ok", score=1.0, source="probe")]

    state = turn(roster_rows=["row"])
    mp.Budgeter().render([Probe()], state, 100)
    assert seen["turn"] is state
    assert seen["turn"].now_ms == 1_754_700_000_000
    assert seen["turn"].world["roster_rows"] == ["row"]


# --------------------------------------------------------------------------
# base behaviour
# --------------------------------------------------------------------------

def test_no_sources_renders_empty():
    assert mp.Budgeter().render([], turn(), 500) == ""


def test_source_with_no_candidates_renders_nothing():
    assert mp.Budgeter().render([Source("empty", [])], turn(), 500) == ""


def test_everything_fits_no_footer_and_descending_order():
    source = Source("s", [cand("mid", 0.5), cand("top", 0.9), cand("low", 0.1),
                          cand("high", 0.7)])
    rendered = mp.Budgeter().render([source], turn(), 500)
    assert rendered == "top\nhigh\nmid\nlow"
    assert footers(rendered) == {}, "nothing dropped, so no rule-4 line"


def test_ranking_is_by_descending_score_and_drops_are_lowest():
    texts = [f"item-{index:02d}-{'x' * 12}" for index in range(10)]
    scores = [0.31, 0.95, 0.12, 0.77, 0.50, 0.88, 0.05, 0.63, 0.41, 0.99]
    source = Source("s", [cand(text, score)
                          for text, score in zip(texts, scores)])
    rendered = mp.Budgeter().render([source], turn(), 100)
    shown, total = footers(rendered)["s"]
    assert total == 10
    expected = [text for _, text in
                sorted(zip(scores, texts), key=lambda pair: -pair[0])]
    assert body_lines(rendered, "item-") == expected[:shown]
    assert 0 < shown < 10


def test_render_is_stateless_and_repeatable():
    sources = [Source("a", [cand(f"a-{index} {'y' * 14}", 1.0 - index / 50)
                            for index in range(20)]),
               Source("b", [cand(f"b-{index} {'z' * 14}", 1.0 - index / 50)
                            for index in range(20)])]
    budgeter = mp.Budgeter()
    first = budgeter.render(sources, turn(), 240)
    second = budgeter.render(sources, turn(), 240)
    assert first == second


# --------------------------------------------------------------------------
# rule 1: pinned
# --------------------------------------------------------------------------

def test_pinned_survives_extreme_budget_pressure():
    directive = "PINNED WORLD RULE: never invent your own status"
    source = Source("s", [cand("filler-one xxxxxxxx", 0.9),
                          cand(directive, 0.0, pinned=True),
                          cand("filler-two xxxxxxxx", 0.8),
                          cand("filler-three xxxxxx", 0.7)])
    rendered = mp.Budgeter().render([source], turn(), 5)
    assert directive in rendered.splitlines(), "rule 1 outranks the cap"
    assert "filler-one" not in rendered
    assert footers(rendered)["s"] == (1, 4), "pinned counts as shown"


def test_pinned_are_emitted_first_across_sources():
    alpha = Source("alpha", [cand("alpha-body xxxxxxxxxx", 0.99)])
    beta = Source("beta", [cand("beta-body xxxxxxxxxxxx", 0.5),
                           cand("BETA-PINNED-DIRECTIVE", 0.1, pinned=True)])
    rendered = mp.Budgeter().render([alpha, beta], turn(), 500)
    lines = rendered.splitlines()
    assert lines[0] == "BETA-PINNED-DIRECTIVE", "pinned precede every scored line"
    assert set(lines[1:]) == {"alpha-body xxxxxxxxxx", "beta-body xxxxxxxxxxxx"}


def test_pinned_counted_in_footer_counts():
    source = Source("s", [cand("PINNED-HEAD xxxxxxxx", 0.0, pinned=True)]
                    + [cand(f"body-{index} {'x' * 12}", 1.0 - index / 10)
                       for index in range(5)])
    rendered = mp.Budgeter().render([source], turn(), 90)
    shown, total = footers(rendered)["s"]
    assert total == 6
    assert shown == 1 + len(body_lines(rendered, "body-"))
    assert "PINNED-HEAD" in rendered


# --------------------------------------------------------------------------
# rules 2 + 3: weighted shares and redistribution
# --------------------------------------------------------------------------

def test_under_used_share_is_released_to_others():
    tiny = Source("tiny", [cand("tiny-1 xxx", 0.9), cand("tiny-2 xxx", 0.8)])
    bulk = Source("bulk", [cand(f"bulk-{index:02d} {'x' * 12}", 1.0 - index / 100)
                           for index in range(50)])
    rendered = mp.Budgeter().render([tiny, bulk], turn(), 400)
    assert len(body_lines(rendered, "tiny-")) == 2
    assert "tiny" not in footers(rendered), "fully shown sources get no footer"
    # A strict half-share (200 chars / 21-char lines) caps bulk at 9 lines;
    # rule 3 hands it tiny's unused ~178 chars as well.
    assert len(body_lines(rendered, "bulk-")) >= 15
    assert len(rendered) <= 400


def test_weight_scales_the_share():
    heavy = Source("heavy", [cand(f"h-{index:02d} xxxxx", 1.0 - index / 100)
                             for index in range(60)], weight=3.0)
    light = Source("light", [cand(f"l-{index:02d} xxxxx", 1.0 - index / 100)
                             for index in range(60)], weight=1.0)
    rendered = mp.Budgeter().render([heavy, light], turn(), 440)
    shown_heavy = len(body_lines(rendered, "h-"))
    shown_light = len(body_lines(rendered, "l-"))
    assert shown_light > 0
    assert 2.0 <= shown_heavy / shown_light <= 4.5, \
        f"3:1 weights should give roughly 3x the lines, got {shown_heavy}:{shown_light}"
    assert len(rendered) <= 440


def test_zero_weight_source_gets_only_leftovers():
    weighted = Source("main", [cand(f"main-{index} {'x' * 13}", 1.0 - index / 10)
                               for index in range(5)], weight=1.0)
    zero = Source("zero", [cand(f"zero-{index:02d} {'x' * 11}", 1.0 - index / 100)
                           for index in range(20)], weight=0.0)
    rendered = mp.Budgeter().render([weighted, zero], turn(), 400)
    assert len(body_lines(rendered, "main-")) == 5, "weighted source fills first"
    assert len(body_lines(rendered, "zero-")) > 0, "leftovers still reach it"
    assert len(rendered) <= 400


def test_redistribution_stall_falls_back_to_global_best_fit():
    # Each candidate is bigger than a half-share but fits the whole pool: the
    # weighted rounds stall at zero consumption and must terminate, then the
    # leftovers are granted by global score. The winner is the higher-scored
    # source; the loser is reported, not silently cut.
    hi = Source("hi", [cand("H" * 60, 0.9)])
    lo = Source("lo", [cand("L" * 60, 0.2)])
    rendered = mp.Budgeter().render([hi, lo], turn(), 100)
    assert rendered.splitlines() == ["H" * 60, "[lo: 0 of 1 shown]"]
    assert len(rendered) <= 100


def test_all_sources_exhausted_terminates_with_budget_to_spare():
    sources = [Source("a", [cand("a-line xxxx", 0.9)]),
               Source("b", [cand("b-line xxxx", 0.8)])]
    rendered = mp.Budgeter().render(sources, turn(), 10_000)
    assert rendered == "a-line xxxx\nb-line xxxx"


# --------------------------------------------------------------------------
# rule 4: dropped counts, never a silent cut
# --------------------------------------------------------------------------

def test_dropped_counts_are_accurate_across_sources():
    alpha = Source("alpha", [cand(f"A-{index} {'x' * 18}", 1.0 - index / 20)
                             for index in range(7)])
    beta = Source("beta", [cand(f"B-{index} {'x' * 18}", 1.0 - index / 20)
                           for index in range(9)])
    rendered = mp.Budgeter().render([alpha, beta], turn(), 260)
    reported = footers(rendered)
    for name, prefix, total in (("alpha", "A-", 7), ("beta", "B-", 9)):
        shown_lines = len(body_lines(rendered, prefix))
        assert name in reported, f"{name} dropped items but has no footer"
        assert reported[name] == (shown_lines, total)
        assert shown_lines < total
    assert len(rendered) <= 260


def test_default_footer_exact_format():
    source = Source("alpha", [cand(f"row-{index} {'x' * 14}", 1.0 - index / 10)
                              for index in range(6)])
    rendered = mp.Budgeter().render([source], turn(), 80)
    shown = len(body_lines(rendered, "row-"))
    assert f"[alpha: {shown} of 6 shown]" in rendered.splitlines()


def test_custom_summary_for_dropped_is_used():
    source = Source("mem", [cand(f"mem-{index} {'x' * 14}", 1.0 - index / 10)
                            for index in range(6)],
                    summary=lambda shown, total:
                    f"[mem: {shown} of {total} shown, oldest first]")
    rendered = mp.Budgeter().render([source], turn(), 80)
    shown = len(body_lines(rendered, "mem-"))
    assert f"[mem: {shown} of 6 shown, oldest first]" in rendered.splitlines()


def test_broken_summary_falls_back_to_default():
    def boom(shown, total):
        raise ValueError("footer writer is broken")

    source = Source("s", [cand(f"row-{index} {'x' * 14}", 1.0 - index / 10)
                          for index in range(6)], summary=boom)
    rendered = mp.Budgeter().render([source], turn(), 80)
    shown = len(body_lines(rendered, "row-"))
    assert f"[s: {shown} of 6 shown]" in rendered.splitlines()


def test_zero_budget_still_reports_the_drop():
    source = Source("s", [cand("aaaaaaaaaa", 0.9), cand("bbbbbbbbbb", 0.5),
                          cand("cccccccccc", 0.1)])
    rendered = mp.Budgeter().render([source], turn(), 0)
    assert rendered == "[s: 0 of 3 shown]", "rule 4 outranks the cap"


# --------------------------------------------------------------------------
# the cap
# --------------------------------------------------------------------------

@pytest.mark.parametrize("budget", list(range(120, 1500, 97)))
def test_total_output_never_exceeds_budget(budget):
    sources = [
        Source("alpha", [cand("PINNED-NOTE xxx", 1.0, pinned=True)]
               + [cand(f"alpha-{index} {'x' * 11}", 1.0 - index / 30)
                  for index in range(12)]),
        Source("beta", [cand(f"beta-{index:02d} {'y' * 17}", 1.0 - index / 60)
                        for index in range(30)], weight=2.0),
        Source("gamma", [cand(f"g-{index} xxxx", 0.5 - index / 20)
                         for index in range(5)]),
    ]
    rendered = mp.Budgeter().render(sources, turn(), budget)
    assert len(rendered) <= budget
    assert "PINNED-NOTE xxx" in rendered


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------

def test_broken_source_is_reported_not_fatal():
    class Broken:
        name = "bad"

        def candidates(self, received):
            raise RuntimeError("upstream exploded")

    good = Source("good", [cand("good-line xxxxxxxx", 0.9)])
    rendered = mp.Budgeter().render([Broken(), good], turn(), 200)
    assert "[bad: unavailable]" in rendered.splitlines()
    assert "good-line xxxxxxxx" in rendered.splitlines()
    assert len(rendered) <= 200


def test_non_candidate_items_are_skipped():
    class Sloppy:
        name = "sloppy"

        def candidates(self, received):
            return [mp.Candidate(text="real-one xxxx", score=0.9, source="sloppy"),
                    12345,
                    mp.Candidate(text="real-two xxxx", score=0.8, source="sloppy")]

    rendered = mp.Budgeter().render([Sloppy()], turn(), 500)
    assert rendered == "real-one xxxx\nreal-two xxxx"
    assert footers(rendered) == {}, "junk items are not counted as dropped"


# --------------------------------------------------------------------------
# the regression: 284-agent roster under a small budget
# --------------------------------------------------------------------------

def test_roster_284_under_small_budget_shows_top_scored_and_accurate_footer():
    rows = [f"agent-{index:03d} unspoken dist={100 + index}"
            for index in range(284)]
    budget = 400  # a fifth of the old _cap() limit that caused the defect
    rendered = mp.Budgeter().render([RosterSource()], turn(roster_rows=rows),
                                    budget)

    assert len(rendered) <= budget

    reported = footers(rendered)
    assert "roster" in reported, "a drop this size must never be silent"
    shown, total = reported["roster"]
    assert total == 284
    assert shown >= 8, "a small budget must still carry a useful roster slice"

    roster_lines = body_lines(rendered, "agent-")
    assert len(roster_lines) == shown, "the footer must count what is shown"
    assert roster_lines == rows[:shown], \
        "the survivors must be the highest-scored, not the first by position"
    assert "agent-283" not in rendered

    footer_line = rendered.splitlines()[-1]
    assert footer_line == (f"[roster: {shown} of 284 shown, "
                           f"ranked by unspoken-then-nearest]")
