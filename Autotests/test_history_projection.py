"""History projection: repetition must not crowd out the record.

Tail-truncation selects by position, so an agent stuck repeating one failing
command fills its own context with copies of its mistake. Measured live: the
30000-byte tail held the same wrong trade command 13 times and the correct
form 0 times.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import helper  # noqa: E402


def _history(turns):
    return "".join(f'("2026-08-10 12:{i:02d}:00" \n {body} \n)\n'
                   for i, body in enumerate(turns))


def test_repetition_is_capped(tmp_path):
    turns = ['((mcity-trade "to_go_food 50 Mart"))'] * 20
    f = tmp_path / "h.metta"; f.write_text(_history(turns))
    out = helper.rankedHistory(str(f), 100000)
    assert out.count('to_go_food 50 Mart') <= 2, out


def test_novel_turns_always_survive(tmp_path):
    turns = ['((mcity-needs))'] * 30 + ['((mcity-trade "crystal 50 Mart"))']
    f = tmp_path / "h.metta"; f.write_text(_history(turns))
    out = helper.rankedHistory(str(f), 100000)
    assert 'crystal 50 Mart' in out
    assert out.count('(mcity-needs)') <= 2


def test_budget_is_respected_and_drop_is_declared(tmp_path):
    turns = [f'((mcity-speak "hello {i}"))' for i in range(200)]
    f = tmp_path / "h.metta"; f.write_text(_history(turns))
    out = helper.rankedHistory(str(f), 1200)
    assert len(out) <= 1200 + 200          # header allowance
    assert "of 200 turns shown" in out


def test_output_stays_chronological(tmp_path):
    turns = [f'((mcity-speak "m{i}"))' for i in range(5)]
    f = tmp_path / "h.metta"; f.write_text(_history(turns))
    out = helper.rankedHistory(str(f), 100000)
    assert out.index("m0") < out.index("m4")


def test_missing_file_is_not_fatal():
    assert helper.rankedHistory("/nonexistent/history.metta", 30000) == ""


def test_untimestamped_content_falls_back_to_tail(tmp_path):
    f = tmp_path / "h.metta"; f.write_text("no blocks here at all")
    assert "no blocks" in helper.rankedHistory(str(f), 30000)


def test_idle_reports_are_not_fed_back_as_examples(tmp_path):
    """After a prompt edit weakened the ban on them, the agent emitted
    (send "No new input received.") 90 times in four minutes, and restoring the
    prompt text alone did not stop it: the newest turns in its own history were
    all idle reports, and it copies its last turn. Repetition capping cannot fix
    that - two shown copies are still the two most recent things it did."""
    import helper
    history = tmp_path / "history.metta"
    history.write_text(
        '("2026-08-11 10:00:00" \n ((mcity-work)) \n)\n'
        + '("2026-08-11 10:00:10" \n ((send "No new input received.")) \n)\n' * 20,
        encoding="utf-8")
    body = helper.rankedHistory(str(history), 30000)
    assert "No new input" not in body, "an idle report must never be an exemplar"
    assert "mcity-work" in body, "the real turn must survive"


def test_a_send_that_says_something_is_kept(tmp_path):
    """Narrow by design: a send answering a real message, or reporting a world
    outcome, is what the channel exists for."""
    import helper
    history = tmp_path / "history.metta"
    history.write_text(
        '("2026-08-11 10:00:00" \n ((send "Traded 50 crystal, confirmed by the world")) \n)\n'
        '("2026-08-11 10:00:10" \n ((send "Yes - I am at the hacker house tonight")) \n)\n',
        encoding="utf-8")
    body = helper.rankedHistory(str(history), 30000)
    assert "Traded 50 crystal" in body
    assert "hacker house tonight" in body


def test_a_mixed_turn_is_kept(tmp_path):
    """Only turns whose ENTIRE content was a do-nothing send are dropped."""
    import helper
    history = tmp_path / "history.metta"
    history.write_text(
        '("2026-08-11 10:00:00" \n ((mcity-work) (send "No new input received.")) \n)\n',
        encoding="utf-8")
    assert "mcity-work" in helper.rankedHistory(str(history), 30000)


def test_a_retired_skill_is_not_taught_back_to_the_agent(tmp_path):
    """mcity-areas was unregistered and still ran 27 times in the next window,
    because the agent's own recent turns were full of it. Feeding those back
    teaches exactly the habit that removing the skill was meant to end."""
    import helper
    history = tmp_path / "history.metta"
    history.write_text(
        '("2026-08-11 10:00:00" \n ((mcity-speak "user-agent-x hello there")) \n)\n'
        + '("2026-08-11 10:00:10" \n ((mcity-areas)) \n)\n' * 15,
        encoding="utf-8")
    body = helper.rankedHistory(str(history), 30000)
    assert "mcity-areas" not in body
    assert "mcity-speak" in body, "the real turn must survive"


def test_a_turn_that_partly_used_a_retired_skill_is_dropped_too(tmp_path):
    """This asserted the opposite and was wrong. Keeping mixed turns left
    ((mcity-areas) (mcity-agents)) in the window, and mcity-areas was still
    called 19 times a window after being unregistered and dropped in its
    all-retired form. A retired call in any position is still an example."""
    import helper
    history = tmp_path / "history.metta"
    history.write_text(
        '("2026-08-11 09:59:00" \n ((mcity-speak "user-agent-x hi there")) \n)\n'
        '("2026-08-11 10:00:00" \n ((mcity-areas) (mcity-work)) \n)\n',
        encoding="utf-8")
    body = helper.rankedHistory(str(history), 30000)
    assert "mcity-areas" not in body
    assert "mcity-speak" in body


def test_a_retired_name_passed_as_an_argument_is_dropped(tmp_path):
    """((mcity-agents "mcity-areas")) appeared verbatim in the live history and
    the model imitates whatever it sees."""
    import helper
    history = tmp_path / "history.metta"
    history.write_text(
        '("2026-08-11 10:00:00" \n ((mcity-agents "mcity-areas")) \n)\n',
        encoding="utf-8")
    assert "mcity-areas" not in helper.rankedHistory(str(history), 30000)


def test_an_unsolicited_self_report_is_not_taught_back(tmp_path):
    """The outbound guard stops these reaching anyone, but the agent still spent
    12 of about 30 turns writing them because its own history was full of them."""
    import helper
    history = tmp_path / "history.metta"
    history.write_text(
        '("2026-08-11 10:00:00" \n ((mcity-work)) \n)\n'
        + '("2026-08-11 10:00:10" \n ((send "I am currently in the '
          'hacker-house-interior, working on a task. My hunger is normal, and I '
          'have earned enough resources. No pending messages to reply to.")) \n)\n' * 8,
        encoding="utf-8")
    body = helper.rankedHistory(str(history), 30000)
    assert "currently in the hacker-house" not in body
    assert "mcity-work" in body


def test_a_real_report_of_an_outcome_is_still_taught(tmp_path):
    """Reporting a confirmed world action is one of the two things send is for."""
    import helper
    history = tmp_path / "history.metta"
    history.write_text(
        '("2026-08-11 10:00:00" \n ((send "Sold 50 crystal to Central Mart, '
        'confirmed by the world; holding 21k meme_coin")) \n)\n',
        encoding="utf-8")
    assert "Sold 50 crystal" in helper.rankedHistory(str(history), 30000)


def test_answering_a_person_is_still_taught(tmp_path):
    import helper
    history = tmp_path / "history.metta"
    history.write_text(
        '("2026-08-11 10:00:00" \n ((send "Yes - the shipment cleared customs '
        'an hour ago, I watched it settle")) \n)\n',
        encoding="utf-8")
    assert "shipment cleared" in helper.rankedHistory(str(history), 30000)


def test_the_malformed_cmd_form_is_not_taught_back(tmp_path):
    """The agent copied the cmd= label as part of the command name and emitted
    (cmd=work) - 90 of 111 decisions in one window were that no-op. The label is
    gone from the harness, but its examples remain in history."""
    import helper
    history = tmp_path / "history.metta"
    history.write_text(
        '("2026-08-11 10:00:00" \n ((mcity-work)) \n)\n'
        + '("2026-08-11 10:00:10" \n ((cmd=work)) \n)\n' * 10,
        encoding="utf-8")
    body = helper.rankedHistory(str(history), 30000)
    assert "cmd=work" not in body
    assert "mcity-work" in body
