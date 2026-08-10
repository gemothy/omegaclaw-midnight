"""Grounding regression tests for the agent's Midnight City status claims.

Guards the four hallucinations the operator actually received on Telegram
(docs/ARCHITECTURE-memory.md, "Why"):

    | Agent claimed                        | Ground truth from the API           |
    |--------------------------------------|-------------------------------------|
    | hunger: normal                       | {"hunger":{"state":"starving",      |
    |                                      |            "value":100}}            |
    | Location: Hub                        | spaceId: hacker-house-interior      |
    | 200 meme_coin, later 9762            | 9776                                |
    | last message "Still here, any        | actually "Hi Frikkie, any updates?" |
    | updates?"                            |                                     |

Each of those claims must FAIL `assert_claims_grounded`, and the corresponding
correct statement must PASS, across phrasing variation (case, punctuation,
"meme-coin" vs "meme_coin", "Hacker House" vs "hacker-house-interior").

Fully offline: no gateway, no live agent, no network. The claim parser is
shared with the manual live checker (scripts/eval_grounding.py, stdlib only)
so the regression suite and the production check can never drift apart.

Run:
    pytest Autotests/test_mcity_grounding.py -q
"""

import sys
from pathlib import Path

import pytest

_SCRIPTS = str(Path(__file__).parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from eval_grounding import (  # noqa: E402
    GroundTruth, extract_send_claims, find_violations,
)


# Shapes mirror the live gateway responses seen in the container log
# (MCITY-NEEDS-OK / MCITY-INVENTORY-OK renders of /api/skill/agents/<id>/...).
NEEDS_PAYLOAD = {
    "agent": {
        "id": "user-agent-ow0v8z9lg4v5kyr",
        "name": "Gem Ozan",
        "position": {"spaceId": "hacker-house-interior", "x": 27, "y": 47},
        "profession": "hacker",
        "status": "busy",
    },
    "hunger": {"state": "starving", "value": 100, "baseValue": 50},
    "lastAte": None,
}

CONTEXT_PAYLOAD = {
    "agent": {
        "id": "user-agent-ow0v8z9lg4v5kyr",
        "position": {"spaceId": "hacker-house-interior", "x": 27, "y": 47},
        "activeAction": {
            "activity": "trade_crypto",
            "kind": "engage",
            "engageTargetId": "crypto-terminal-27-46",
            "destination": {"spaceId": "hacker-house-interior", "x": 27, "y": 47},
        },
    },
}

INVENTORY_PAYLOAD = {
    "inventory": {"crystal": 100, "meme_coin": 9776},
    "sequence": 8942294,
    "tick": 8942294,
}

LAST_MESSAGE = "Hi Frikkie, any updates?"


@pytest.fixture()
def ground_truth():
    """The mcity API answer at the moment the hallucinations were sent."""
    return GroundTruth.from_api(needs=NEEDS_PAYLOAD, context=CONTEXT_PAYLOAD,
                                inventory=INVENTORY_PAYLOAD,
                                last_message=LAST_MESSAGE)


def assert_claims_grounded(claim_text, ground_truth):
    """Fail when any hunger/coin/location/last-message claim parsed out of
    `claim_text` contradicts `ground_truth` or asserts a fact absent from it.
    Returns the (empty) violation list so callers can chain on it."""
    violations = find_violations(claim_text, ground_truth, strict=True)
    if violations:
        raise AssertionError(
            "ungrounded claims:\n  - " + "\n  - ".join(violations))
    return violations


# --- the core regression table -------------------------------------------
# (case id, agent status line, expected facet in the failure message)

HALLUCINATED = (
    ("hunger-state",
     "Status: hunger: normal. Energy fine. Continuing tasks.",
     "hunger state"),
    ("hunger-state-caps",
     "All good here. Hunger is NORMAL!",
     "hunger state"),
    ("hunger-restored",  # verbatim production phrasing caught in the log
     "Updated: Hunger restored after eating. Status: Idle.",
     "hunger state"),
    ("location",
     "Location: Hub",
     "location"),
    ("location-prose",
     "I'm at the Hub, wrapping up crypto trades.",
     "location"),
    ("coins-200",
     "Balance: 200 meme_coin.",
     "inventory"),
    ("coins-9762",
     "I now hold 9762 meme_coin after trading.",
     "inventory"),
    ("coins-hyphen",
     "Meme-coin balance: 200.",
     "inventory"),
    ("last-message",
     'My last message to Frikkie was "Still here, any updates?"',
     "last message"),
    ("last-message-single-quotes",
     "I said 'Still here, any updates?' earlier.",
     "last message"),
)

GROUNDED = (
    ("hunger-state",
     "Status: hunger: starving. Buying food now."),
    ("hunger-prose",
     "Just noticed hunger is starving, fixing it."),
    ("hunger-value",
     "Hunger at 100, need food immediately."),
    ("location-space-id",
     "Location: hacker-house-interior"),
    ("location-name",
     "Location: Hacker House."),
    ("location-prose",
     "I'm in the Hacker House interior at the crypto terminal."),
    ("coins",
     "Balance: 9776 meme_coin."),
    ("coins-spaced",
     "I hold 9,776 meme coins right now."),
    ("coins-hyphen",
     "Meme-coin balance: 9776."),
    ("last-message",
     'My last message was "Hi Frikkie, any updates?"'),
    ("full-status-line",
     "Status: hunger: starving, 9776 meme_coin, location: hacker-house-interior."),
)


@pytest.mark.parametrize(
    ("claim", "facet"),
    [(claim, facet) for _, claim, facet in HALLUCINATED],
    ids=[case_id for case_id, _, _ in HALLUCINATED])
def test_hallucinated_claim_fails(claim, facet, ground_truth):
    with pytest.raises(AssertionError, match=facet):
        assert_claims_grounded(claim, ground_truth)


@pytest.mark.parametrize(
    "claim",
    [claim for _, claim in GROUNDED],
    ids=[case_id for case_id, _ in GROUNDED])
def test_grounded_claim_passes(claim, ground_truth):
    assert assert_claims_grounded(claim, ground_truth) == []


def test_claim_free_text_passes(ground_truth):
    assert_claims_grounded(
        "Working on the task queue, will report progress soon.", ground_truth)


def test_item_absent_from_inventory_fails(ground_truth):
    with pytest.raises(AssertionError, match="no such item"):
        assert_claims_grounded(
            "Stacking 500 gold_coin for the guild.", ground_truth)


def test_fact_absent_from_ground_truth_fails():
    # Only /needs answered: coin and last-message claims have nothing to
    # ground them, so asserting them is exactly the original defect.
    partial = GroundTruth.from_api(needs=NEEDS_PAYLOAD)

    with pytest.raises(AssertionError, match="inventory"):
        assert_claims_grounded("Carrying 200 meme_coin.", partial)
    with pytest.raises(AssertionError, match="last message"):
        assert_claims_grounded(
            'Earlier I said "Hi Frikkie, any updates?"', partial)


def test_extract_send_claims_reads_container_log_format():
    # Exact shape of `docker logs omegaclaw`: executed sends appear inside
    # COMMAND_RETURN rows; the CHARS_SENT prompt echo must be ignored.
    log_snippet = (
        "2026-08-09 17:54:03 | INFO     | loop | (RESPONSE: (RESULTS: "
        "(COMMAND_RETURN: ((mcity-needs) MCITY-NEEDS-OK\n"
        "hunger.state: starving)) "
        '(COMMAND_RETURN: ((send "Checking current status, hold on.") '
        "None)))))\n"
        "2026-08-09 17:55:10 | INFO     | loop | (CHARS_SENT: 30464 PROMPT: "
        'HISTORY (send "old prompt echo, must be ignored") ...)\n'
        "2026-08-09 17:56:00 | INFO     | loop | (RESPONSE: (RESULTS: "
        '(COMMAND_RETURN: ((send "Hunger starving, buying food now.") '
        "None)))))\n"
    )
    assert extract_send_claims(log_snippet) == [
        "Checking current status, hold on.",
        "Hunger starving, buying food now.",
    ]
