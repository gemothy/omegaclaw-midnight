"""The outbound ban on idle reports, enforced rather than requested.

The agent is told never to send "no new input" style messages. For most of a long
session it did not - then a prompt edit weakened the wording and it produced 132
of them in eight minutes. Nothing reached the operator only because the Telegram
channel had never learned a chat id. Prose is not a safeguard for something that
rings a real person's phone.

Run:
    OMEGACLAW_SKIP_LIVE_CLEANUP=1 python3 -m pytest Autotests/test_idle_report_guard.py -q
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _path in (_REPO, os.path.join(_REPO, "channels"), os.path.join(_REPO, "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

telegram = pytest.importorskip("telegram", reason="channel deps unavailable")


@pytest.mark.parametrize("text", [
    "No new input received.",
    "no new input",
    "Nothing to report",
    "Nothing new to report.",
    "Standing by",
    "Awaiting your instructions",
    "  No new messages.  ",
    "No updates",
])
def test_a_bare_idle_report_is_suppressed(text):
    assert telegram.is_idle_report(text) is True


@pytest.mark.parametrize("text", [
    "Traded 50 crystal at Central Mart, confirmed by the world",
    "Fergie asked about the crystal shipment; I told her it cleared",
    "No new input from the docks, but I sold 50 crystal and hold 18k meme_coin",
    "I am at the hacker house. Hunger normal, holding 2 to_go_food.",
    "Nothing to report on the harbour contract, but the trade with Rico settled",
])
def test_a_message_with_real_content_is_never_suppressed(text):
    assert telegram.is_idle_report(text) is False, text


def test_the_guard_is_anchored_to_the_whole_message():
    """A real update that merely contains the phrase must survive: the ban is on
    saying nothing, not on the words themselves."""
    assert telegram.is_idle_report("standing by the crystal terminal, sold 12") is False
    assert telegram.is_idle_report("Standing by") is True
