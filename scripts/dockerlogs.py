"""Read container logs by wall-clock window WITHOUT trusting `docker logs --since`.

`--since` is evaluated against the Docker daemon's clock. On this host the daemon
runs four hours behind the container and the host:

    host/container 2026-08-10T17:04:31Z      daemon 2026-08-10T13:04:31

so `--since 1m` returned zero lines while the agent was actively logging every
second. That silently turned both verification scripts into pass-machines: no log
lines means no claims, which reads as "no contradictions found".

A checker that reports success because it looked at nothing is worse than no
checker, so we never ask the daemon to do time arithmetic. We pull `--tail` with
`--timestamps` and filter on the RFC3339 prefix Docker writes into each line,
comparing against OUR clock. `read_window` distinguishes "no lines at all"
(unavailable) from "lines, none in window" (genuinely quiet) so callers can fail
loudly on the former.
"""

import datetime as _dt
import re
import subprocess

# Docker prefixes each line with an RFC3339Nano timestamp when --timestamps is on.
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s?(.*)$")

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(text):
    """'30m' -> 1800 seconds. Accepts s/m/h/d. Returns None if unparseable."""
    match = re.fullmatch(r"(\d+)\s*([smhd])", (text or "").strip().lower())
    if not match:
        return None
    return int(match.group(1)) * _UNITS[match.group(2)]


def read_window(container, window, timeout=30.0, tail=4000, strip_ts=True):
    """Return (text, error).

    error is a string when the logs could not be read at all, when the container
    produced no output whatsoever, or when the retrieved tail is entirely OLDER
    than the requested window. Callers must treat any error as "unavailable",
    never as "clean". A container that is logging but is merely quiet inside the
    window yields ("", None).

    `tail` is deliberately modest. With json-file rotation (max-file=3), large
    --tail values make Docker return an older rotated segment and drop the
    current one entirely - measured on this host:

        --tail   1000 ->   999 lines, newest 17:53:21  (current)
        --tail  50000 -> 11448 lines, newest 10:32:52  (seven hours stale)

    Lines are sorted by their own timestamp before filtering, so out-of-order
    segments cannot hide recent entries either.
    """
    cmd = ["docker", "logs", "--timestamps", "--tail", str(tail), container]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=timeout, check=False)
    except FileNotFoundError:
        return None, "docker CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return None, f"docker logs timed out after {timeout}s"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return None, detail[0] if detail else f"container {container!r} unavailable"

    raw = (proc.stdout or "") + (proc.stderr or "")
    if not raw.strip():
        return None, (f"container {container!r} produced no log output at all "
                      f"(cannot verify anything)")

    seconds = parse_window(window)
    cutoff = None
    if seconds is not None:
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds)

    # (timestamp, [line, continuation...]) so a rotated segment cannot reorder us.
    records, newest = [], None
    for line in raw.splitlines():
        match = _TS.match(line)
        if not match:
            if records:                      # continuation of the previous entry
                records[-1][1].append(line)
            continue
        try:
            when = _dt.datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
        newest = when if newest is None or when > newest else newest
        records.append((when, [match.group(2) if strip_ts else line]))

    if not records:
        # No parseable timestamps: return everything rather than silently nothing.
        return raw, None

    if cutoff is not None and newest is not None and newest < cutoff:
        stale = (_dt.datetime.now(_dt.timezone.utc) - newest).total_seconds()
        return None, (f"log tail is stale: newest entry is {stale / 60:.0f} min old, "
                      f"outside the requested {window} window. The container may be "
                      f"silent, or rotation may be hiding the current segment - "
                      f"retry with a smaller --tail.")

    records.sort(key=lambda item: item[0])
    kept = [text for when, block in records
            if cutoff is None or when >= cutoff
            for text in block]
    return "\n".join(kept), None
