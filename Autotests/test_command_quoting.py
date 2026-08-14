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
    """Upstream guarantees inner quotes are meaningful. The repair is narrow on
    purpose: it fires only when the WHOLE argument is double-wrapped."""
    assert helper.balance_parentheses('send "hello" world') \
        == '((send "\\"hello\\" world"))'
    multi = helper.balance_parentheses(
        'send "Plain text version:"\n**Mars** - red planet')
    assert '**Mars**' in multi and '\\"Plain text version:\\"' in multi


def test_only_observed_shapes_are_asserted():
    """Deliberately empty of invented shapes.

    An earlier version of this file asserted behaviour for inputs like
    `pin "\\"status: idle\\")"`, reconstructed from the parser's ERROR OUTPUT
    rather than from the model. Once the raw input was actually logged, the real
    malformation turned out to be an escaped CLOSING quote plus several commands
    on one line - see the two tests below, which use verbatim captures. Asserting
    against reconstructed shapes is how three repair attempts were written for a
    bug that was never happening in that form."""


def test_strip_is_total_and_never_raises():
    for junk in ("", '"', '""', '\\"', '"\\""', None, 5):
        helper.strip_redundant_quotes(junk)


def test_escaped_closing_quote_is_repaired():
    """Captured verbatim from the live agent. The model escapes the CLOSING
    quote, so the string never terminates and MeTTa rejects the whole form.
    Three earlier repair attempts missed this because they were written against
    shapes INFERRED from the post-parse error text rather than the real input."""
    assert helper.balance_parentheses('((pin "status: idle, no input\\"))') \
        == '((pin "status: idle, no input"))'


def test_several_commands_on_one_line_are_split():
    """The model puts several commands on one line inside an extra paren
    wrapper. split_command_blocks divides on NEWLINES, so the whole line arrived
    as one command whose argument swallowed the rest."""
    got = helper.balance_parentheses(
        '((send "No new user input. Checking Midnight City status.\\") (mcity-threads))')
    assert got == '((send "No new user input. Checking Midnight City status.") (mcity-threads))'


def test_single_command_keeps_working():
    assert helper.split_toplevel_groups('(send "one")') == ['(send "one")']
    assert helper.balance_parentheses('(send "one")') == '((send "one"))'
    assert helper.balance_parentheses('mcity-eat') == '((mcity-eat))'


def test_bare_multi_command_line_is_split():
    """Captured live after the wrapped case was fixed. The wrapper is optional:

        (mcity-threads) (mcity-work)

    Requiring '((' missed this, and it parsed as
        (mcity-threads) "(mcity-work"
    with the second command swallowed as a quoted argument of the first."""
    assert helper.balance_parentheses('(mcity-threads) (mcity-work)') \
        == '((mcity-threads) (mcity-work))'
    assert helper.balance_parentheses('((mcity-threads) (mcity-work))') \
        == '((mcity-threads) (mcity-work))'


def test_a_lone_group_is_not_disturbed():
    assert helper.balance_parentheses('(send "one")') == '((send "one"))'
    assert helper.balance_parentheses('(mcity-eat)') == '((mcity-eat))'


def test_the_rules_only_use_escape_tokens_the_harness_translates():
    """_quote_, _apostrophe_ and _newline_ are translated on the way out; anything
    else reaches the model literally. A rule about quoting that itself contained an
    untranslated _backslash_ would teach the token instead of the lesson - which is
    what the first draft of that rule did.

    Checks a denylist rather than every _word_ shape: the SKILLS descriptions are
    full of snake_case parameter names like agent_id_in_quotes, and those are not
    escapes."""
    import pathlib as _p
    metta = (_p.Path(__file__).resolve().parent.parent
             / "plugins/mcity/mcity.metta").read_text()
    never = ("_backslash_", "_tab_", "_space_token_", "_slash_", "_dquote_",
             "_squote_", "_lparen_", "_rparen_", "_bracket_")
    found = [token for token in never if token in metta]
    assert not found, f"untranslated escape tokens in the prompt: {found}"


def test_the_rules_explain_the_history_escaping_the_model_can_see():
    """The recalled history stores a quote character as the token _quote_ - about
    22,000 occurrences in one prompt - while the rules tell the agent to write
    real double quotes. It produced hybrids: (mcity-move-area "\\"central-quote_)")
    failed to parse, 4 times in 190 turns. The rules have to name the difference,
    because the exemplar it sees most often is the one it must not copy."""
    import pathlib as _p
    metta = (_p.Path(__file__).resolve().parent.parent
             / "plugins/mcity/mcity.metta").read_text()
    rules = metta[metta.index("MIDNIGHT_CITY_RULES"):]
    assert "HISTORY above stores" in rules, (
        "the rules must explain the _quote_ token the model reads constantly")
