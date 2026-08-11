"""
Session-level cleanup: after the entire pytest session finishes, re-run the
legacy cleanup to catch any test artifacts the agent wrote AFTER per-test
teardown ran (agent is autonomous and may produce `remember` calls with a
delay).

This fixture reaches into the RUNNING agent container as root and rewrites its
history and vector store. That is intentional for live end-to-end runs, but it
must not fire for hermetic unit tests: it costs 15s per session, mutates
production state, and hard-fails on any machine without a live agent.

It is therefore skipped unless a live agent is actually reachable. Force it off
with OMEGACLAW_SKIP_LIVE_CLEANUP=1.
"""
import os
import shutil
import subprocess
import time

import pytest

from cleanup_legacy import LEGACY_MARKERS
from helpers import (
    chromadb_cleanup_by_markers, history_cleanup_by_markers,
)

_CONTAINER = os.environ.get("OMEGACLAW_CONTAINER", "omegaclaw")


def _live_agent_present():
    """True only if we can actually see a running agent container."""
    if os.environ.get("OMEGACLAW_SKIP_LIVE_CLEANUP"):
        return False
    if shutil.which("docker") is None:
        return False
    try:
        done = subprocess.run(
            ["docker", "ps", "--filter", f"name=^{_CONTAINER}$",
             "--filter", "status=running", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and _CONTAINER in (done.stdout or "")


@pytest.fixture(scope="session", autouse=True)
def _post_session_cleanup():
    yield
    if not _live_agent_present():
        return
    print("\n>> post-session cleanup (grace period 15s)", flush=True)
    time.sleep(15)
    try:
        h = history_cleanup_by_markers(LEGACY_MARKERS)
        c = chromadb_cleanup_by_markers(LEGACY_MARKERS)
    except Exception as exc:                      # never fail a green session
        print(f"   [final] live cleanup skipped: {exc}", flush=True)
        return
    print(f"   [final] history={h} blocks, chromadb={c} vectors", flush=True)


@pytest.fixture(autouse=True)
def _reset_mcity_module_state():
    """mcity_client keeps grounding and read-dedup state in module globals, so a
    test that reads twice can be suppressed by what an EARLIER test read. Two
    roster tests here passed alone and failed in suite order for exactly that
    reason. tests/mcity/conftest.py holds the matching fixture."""
    try:
        import mcity_client as mc
    except ImportError:                 # suites that never load the plugin
        yield
        return
    pristine = {"at_ms": 0, "hunger": None, "space": None, "items": None,
                "status": None, "busy_for": None,
                    "engaged": False,
                    "district": None}
    mc._ASLEEP.clear()
    mc._CAN_SPEAK.clear()
    mc._AWAKE_PLACES.clear()
    mc._can_speak_at_ms = 0
    mc._dnd_streak = 0
    mc._last_self_probe_ms = 0
    mc._waiting_refresh_at_ms = 0
    mc._waiting_refreshing = False
    mc._can_speak_refreshing = False
    mc._LAST_READ.clear()
    mc._WAITING.update({'at_ms': 0, 'ids': []})
    mc._VITALS.clear(); mc._VITALS.update(pristine)
    mc._vitals_refreshing = False
    yield
    mc._LAST_READ.clear()
    mc._VITALS.clear(); mc._VITALS.update(pristine)
    mc._vitals_refreshing = False
