"""The command parser must forgive the model re-quoting a quoted argument.

Measured live over one hour: 51 usable decisions against 65 syntax errors. More
than half of every turn was discarded before anything ran, and the failures were
all one shape - an argument wrapped in escaped quotes:

    send "\\"Awaiting input.\\""   ->  MeTTa: Parse error in form
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import helper  # noqa: E402


def test_escaped_quote_wrapper_is_stripped():
    assert helper.balance_parentheses('send "\\"Awaiting input.\\""') \
        == '((send "Awaiting input."))'
    assert helper.balance_parentheses('(send "\\"Gem: Status normal.\\"")') \
        == '((send "Gem: Status normal."))'
    assert helper.balance_parentheses('pin "\\"status: idle\\""') \
        == '((pin "status: idle"))'


def test_wellformed_arguments_are_untouched():
    """The forgiving path must never corrupt input that was already correct."""
    assert helper.balance_parentheses('send "Awaiting input."') \
        == '((send "Awaiting input."))'
    assert helper.balance_parentheses('mcity-trade "crystal 50 Central Mart Outlet"') \
        == '((mcity-trade "crystal 50 Central Mart Outlet"))'
    assert helper.balance_parentheses('mcity-eat') == '((mcity-eat))'


def test_inner_quotes_that_are_not_a_wrapper_survive():
    """Only a wrapper is stripped; quotes that are part of the message stay."""
    out = helper.balance_parentheses('send "he said \\"hi\\" to me"')
    assert 'hi' in out
    assert out.startswith('((send "') and out.endswith('"))')


def test_strip_is_total_and_never_raises():
    for junk in ("", '"', '""', '\\"', '"\\""', None, 5):
        helper.strip_redundant_quotes(junk)
