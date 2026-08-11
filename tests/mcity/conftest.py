import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
for _path in (os.path.join(_REPO, "plugins", "mcity"), _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import mcity_client as mc                       # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_state():
    """mcity_client keeps grounding and dedup state in module globals, so a test
    can be changed by what an earlier one did. The reset lives in the module
    itself: three caches in a row caused order-dependent failures because a new
    one was added there and this file was not updated to match."""
    cfg = dict(mc._cfg)
    mc.reset_runtime_state()
    yield
    mc.reset_runtime_state()
    mc._cfg.clear()
    mc._cfg.update(cfg)
