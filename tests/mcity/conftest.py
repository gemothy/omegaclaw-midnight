"""Isolate the mcity client's module-level state between tests.

mcity_client keeps grounding state in module globals (_VITALS above all), and
tests in both suites write to them directly. Nothing restored them, so the two
suites passed alone - 142 and 105 - and failed 11 when run in one process,
purely from order. A left-over status=busy is enough to make an unrelated test's
speak refuse. Reset before every test so the suites compose."""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
for _path in (os.path.join(_REPO, "plugins", "mcity"), _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import mcity_client as mc                       # noqa: E402

_PRISTINE_VITALS = {"at_ms": 0, "hunger": None, "space": None, "items": None,
                    "status": None, "busy_for": None}


@pytest.fixture(autouse=True)
def _reset_module_state():
    cfg = dict(mc._cfg)
    mc._VITALS.clear()
    mc._VITALS.update(_PRISTINE_VITALS)
    mc._vitals_refreshing = False
    mc._last_busy_probe_ms = 0
    mc._LAST_READ.clear()
    mc._WAITING.update({'at_ms': 0, 'ids': []})
    yield
    mc._VITALS.clear()
    mc._VITALS.update(_PRISTINE_VITALS)
    mc._vitals_refreshing = False
    mc._last_busy_probe_ms = 0
    mc._LAST_READ.clear()
    mc._WAITING.update({'at_ms': 0, 'ids': []})
    mc._cfg.clear()
    mc._cfg.update(cfg)
