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
