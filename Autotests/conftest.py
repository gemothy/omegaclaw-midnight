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
