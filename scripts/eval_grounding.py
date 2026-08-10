#!/usr/bin/env python3
"""Grounding checker for the Midnight City agent's outbound `send` claims.

The agent once told its operator on Telegram that hunger was `normal` while the
world API said `{"hunger":{"state":"starving","value":100}}`, that it was at
the `Hub` while `spaceId` was `hacker-house-interior`, that it held `200` (and
later `9762`) meme_coin against an actual `9776`, and misquoted its own last
message (see docs/ARCHITECTURE-memory.md, "Why"). This module holds the ONE
claim parser used both by the offline regression suite
(Autotests/test_mcity_grounding.py) and by the manual live checker below, so
the two can never drift apart.

Offline core (no network, importable from tests):
    GroundTruth.from_api(needs=..., context=..., inventory=..., last_message=...)
    find_violations(claim_text, truth, strict=...)  -> list of violation strings
    extract_send_claims(docker_log_text)            -> list of sent messages

Live checker (manual, never wired into CI):
    python3 scripts/eval_grounding.py [--agent ID] [--since 30m] ...

  1. reads the agent's recent `send` claims out of `docker logs <container>`,
  2. fetches ground truth from the local gateway
       GET <gateway>/mcity/api/skill/agents/<AGENT_ID>/needs
       GET <gateway>/mcity/api/skill/agents/<AGENT_ID>/context
       GET <gateway>/mcity/api/skill/agents/<AGENT_ID>/inventory
     (read-only skill routes; the nginx gateway injects the master token, this
     process never sees a credential and never prints one),
  3. reports every contradiction.

Exit codes:
    0  every parsed claim is consistent with the world
    1  at least one contradiction was found
    2  ground truth or the container log is unavailable (clear message printed)

Caveat: the world moves while the log window ages - hunger and coin counts
drift, so keep `--since` short and use `--coin-tolerance` during busy trading
periods. Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_GATEWAY_URL = "http://localhost:8080"
DEFAULT_CONTAINER = "omegaclaw"
DEFAULT_SINCE = "30m"
DEFAULT_HTTP_TIMEOUT = 10.0
DEFAULT_MAX_CLAIMS = 20
MAX_RESPONSE_BYTES = 5_000_000
USER_AGENT = "OmegaClaw-eval-grounding/1.0"

EXIT_OK = 0
EXIT_CONTRADICTION = 1
EXIT_UNAVAILABLE = 2

ENDPOINTS = ("needs", "context", "inventory")

# Mirrors plugins/mcity/mcity_client.py:ID_RE - never let a malformed id build
# a URL path, and never print anything that looks like a credential.
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TOKEN_SCRUB_RE = re.compile(r"midnight_[A-Za-z0-9_\-]{4,}")
_BEARER_SCRUB_RE = re.compile(r"(?i)bearer\s+\S+")

# ---------------------------------------------------------------------------
# text normalisation
# ---------------------------------------------------------------------------

# Fold the MeTTa prompt manglings and smart quotes before parsing: claims read
# out of the container log may carry either form.
_FOLDS = (
    ("_newline_", " "), ("_apostrophe_", "'"), ("_quote_", '"'),
    ("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"),
)

_WORD_RE = re.compile(r"[a-z0-9]+")

# Dropped when comparing location phrases: "at the Hacker House" and
# "hacker-house-interior" must meet on {hacker, house}.
_LOC_STOPWORDS = frozenset((
    "the", "a", "an", "at", "in", "on", "of", "my", "our", "near", "by",
    "into", "inside", "now", "currently", "still", "right", "here", "area",
    "zone",
))


def _preprocess(text):
    out = text if isinstance(text, str) else str(text)
    for old, new in _FOLDS:
        out = out.replace(old, new)
    return out


def _norm_join(text):
    """Case/punctuation-insensitive form: 'Hi, Frikkie!' -> 'hi frikkie'."""
    return " ".join(_WORD_RE.findall(text.lower()))


def _sig_tokens(text):
    return [tok for tok in _WORD_RE.findall(text.lower())
            if tok not in _LOC_STOPWORDS]


def _squash(text):
    """'Meme-Coin' / 'meme coin' / 'meme_coin' -> 'memecoin'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


# ---------------------------------------------------------------------------
# claim extraction
# ---------------------------------------------------------------------------

# hunger: normal | Hunger is NORMAL | hunger state: hungry | hunger at 100
_HUNGER_RE = re.compile(
    r"\bhunger\b[\s:=\-]*"
    r"(?:(?:level|state|status|value|is|was|reads|seems|looks|now|at|"
    r"currently|about|around)[\s:=\-]+){0,3}"
    r"([a-z]+|\d[\d,]{0,3})",
    re.IGNORECASE)
_FEELING_RE = re.compile(
    r"\b(?:i\s+am|i'm|im|feeling)\s+"
    r"(starving|starved|famished|hungry|peckish|full)\b",
    re.IGNORECASE)

# Coarse states the agent may report, folded onto the world vocabulary.
_HUNGER_STATES = {
    "normal": "normal", "fine": "normal", "ok": "normal", "okay": "normal",
    "good": "normal", "satisfied": "normal", "sated": "normal",
    "fed": "normal", "stable": "normal", "nominal": "normal",
    "restored": "normal", "recovered": "normal",
    "hungry": "hungry", "peckish": "hungry",
    "starving": "starving", "starved": "starving", "famished": "starving",
    "critical": "starving",
    "full": "full",
}

# 200 meme_coin | 9,776 meme coins | Meme-coin balance: 9776 | 100 crystal
_ITEM_WORD = r"(?:[a-z][a-z0-9]*[\s_\-]+){0,2}[a-z0-9]*(?:coin|crystal)s?"
_COIN_A_RE = re.compile(
    r"\b(\d[\d,]{0,12})\s*(?:x\s+)?(" + _ITEM_WORD + r")\b", re.IGNORECASE)
_COIN_B_RE = re.compile(
    r"\b(" + _ITEM_WORD + r")\b"
    r"(?:\s+(?:balance|count|holdings?|total|stash|reserve))?"
    r"[\s:=\-]*(?:(?:is|was|at|now|currently|stands|sits)[\s:=\-]+){0,2}"
    r"(\d[\d,]{0,12})\b",
    re.IGNORECASE)

# Location: Hub | I'm at the Hub | currently in hacker-house-interior |
# spaceId: hub  - capture at most four words so one claim stays one place.
_WORDS4 = r"((?:[\w'\-]+[ \t]+){0,3}[\w'\-]+)"
_LOC_LABEL_RE = re.compile(
    r"\b(?:location|position|place)\b\s*(?:is|was|:|=|-)?\s*(?:the\s+)?"
    + _WORDS4, re.IGNORECASE)
_LOC_PROSE_RE = re.compile(
    r"\b(?:i\s+am|i'm|im|we\s+are|currently|now|still)\s+(?:at|in|inside)\s+"
    r"(?:the\s+)?" + _WORDS4, re.IGNORECASE)
_LOC_SPACE_RE = re.compile(r"\bspace\s*_?\s*id\b\s*[:=]?\s*([\w\-]+)",
                           re.IGNORECASE)

# last message ... "..." | I said '...'
_QUOTE_RES = (
    re.compile(r"\blast\s+message\b[^\"']{0,60}\"([^\"]{1,400})\""),
    re.compile(r"\blast\s+message\b[^\"']{0,60}'([^']{1,400})'"),
    re.compile(r"(?i)\bi\s+(?:said|sent|wrote|messaged|replied|told\s+[\w'\-]+)"
               r"\b[^\"']{0,30}\"([^\"]{1,400})\""),
    re.compile(r"(?i)\bi\s+(?:said|sent|wrote|messaged|replied|told\s+[\w'\-]+)"
               r"\b[^\"']{0,30}'([^']{1,400})'"),
)


def extract_hunger_claims(text):
    """-> list of state words (str) and/or claimed values (int)."""
    claims = []
    for match in _HUNGER_RE.finditer(text):
        token = match.group(1).lower()
        if token[0].isdigit():
            claims.append(int(token.replace(",", "")))
        elif token in _HUNGER_STATES:
            claims.append(token)
    for match in _FEELING_RE.finditer(text):
        claims.append(match.group(1).lower())
    return claims


def extract_coin_claims(text):
    """-> list of (item_phrase, claimed_count)."""
    claims = []
    for pattern, number_first in ((_COIN_A_RE, True), (_COIN_B_RE, False)):
        for match in pattern.finditer(text):
            number, item = (match.group(1), match.group(2)) if number_first \
                else (match.group(2), match.group(1))
            entry = (item.strip().lower(), int(number.replace(",", "")))
            if entry not in claims:
                claims.append(entry)
    return claims


def extract_location_claims(text):
    claims = []
    for pattern in (_LOC_LABEL_RE, _LOC_PROSE_RE, _LOC_SPACE_RE):
        for match in pattern.finditer(text):
            place = match.group(1).strip()
            if _sig_tokens(place) and place.lower() not in claims:
                claims.append(place.lower())
    return claims


def extract_quote_claims(text):
    claims = []
    for pattern in _QUOTE_RES:
        for match in pattern.finditer(text):
            quoted = match.group(1).strip()
            if quoted and quoted not in claims:
                claims.append(quoted)
    return claims


# In `docker logs`, an executed send shows up inside the loop RESPONSE as
#   (COMMAND_RETURN: ((send "Checking current status, hold on.") None))
# The CHARS_SENT prompt echo may replay older sends, so only COMMAND_RETURN
# rows count, and duplicates are collapsed.
_SEND_RE = re.compile(r'\(COMMAND_RETURN:\s*\(\(send\s+"([^"]*)"\)')


def extract_send_claims(log_text):
    claims = []
    for match in _SEND_RE.finditer(log_text or ""):
        text = match.group(1).strip()
        if text and text not in claims:
            claims.append(text)
    return claims


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------

_LOCATION_KEY_RE = re.compile(
    r"(?i)^(?:space|area|district|building|engage_?target)_?id$")


def _walk_location_names(payload, found, depth=0):
    if depth > 6:
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if (isinstance(value, str) and value.strip()
                    and _LOCATION_KEY_RE.match(str(key))):
                if value not in found:
                    found.append(value)
            else:
                _walk_location_names(value, found, depth + 1)
    elif isinstance(payload, list):
        for item in payload[:50]:
            _walk_location_names(item, found, depth + 1)


def _position_space_id(payload):
    for path in (("agent", "position", "spaceId"), ("position", "spaceId"),
                 ("spaceId",)):
        node = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, str) and node.strip():
            return node
    return None


@dataclass(frozen=True)
class GroundTruth:
    """What the Midnight City API actually said. A `None` facet means that
    piece of ground truth is unavailable, not that it is empty."""

    hunger_state: str | None = None
    hunger_value: int | None = None
    space_id: str | None = None
    location_names: tuple = ()
    inventory: dict | None = None
    last_message: str | None = None

    @classmethod
    def from_api(cls, needs=None, context=None, inventory=None,
                 last_message=None):
        """Build from raw gateway payloads (/needs, /context, /inventory) plus
        the agent's actual last outbound message when known."""
        hunger_state = hunger_value = space_id = None
        if isinstance(needs, dict):
            hunger = needs.get("hunger")
            if isinstance(hunger, dict):
                state = hunger.get("state")
                if isinstance(state, str) and state.strip():
                    hunger_state = state.strip()
                value = hunger.get("value")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    hunger_value = int(value)

        names = []
        for payload in (needs, context):
            if isinstance(payload, dict):
                if space_id is None:
                    space_id = _position_space_id(payload)
                _walk_location_names(payload, names)
        if space_id and space_id in names:
            names.remove(space_id)
        if space_id:
            names.insert(0, space_id)

        counts = None
        if isinstance(inventory, dict):
            raw = inventory.get("inventory", inventory)
            if isinstance(raw, dict):
                counts = {
                    str(key): int(value) for key, value in raw.items()
                    if isinstance(value, (int, float))
                    and not isinstance(value, bool)
                }

        return cls(hunger_state=hunger_state, hunger_value=hunger_value,
                   space_id=space_id, location_names=tuple(names),
                   inventory=counts, last_message=last_message)


# ---------------------------------------------------------------------------
# grounding checks
# ---------------------------------------------------------------------------

def _resolve_item(item_phrase, inventory):
    """Map a claimed item phrase onto an inventory key, tolerant of
    'meme-coin' / 'meme coins' / 'memecoin' / bare 'coins'. None when the
    claim names nothing the agent holds."""
    probe = _squash(item_phrase)
    if not probe:
        return None
    singular = probe[:-1] if probe.endswith("s") else probe
    for key in inventory:
        squashed = _squash(key)
        if probe == squashed or singular == squashed:
            return key
    candidates = []
    for key in inventory:
        squashed = _squash(key)
        if squashed and (singular.endswith(squashed)
                         or squashed.endswith(singular)):
            candidates.append(key)
    return candidates[0] if len(candidates) == 1 else None


def _location_matches(place, names):
    claim_tokens = set(_sig_tokens(place))
    if not claim_tokens:
        return True
    for name in names:
        name_tokens = set(_sig_tokens(name))
        if not name_tokens:
            continue
        needed = min(2, len(claim_tokens), len(name_tokens))
        if len(claim_tokens & name_tokens) >= needed:
            return True
    return False


def _quote_matches(claim, actual):
    claimed = _norm_join(claim)
    if not claimed:
        return True
    truth = _norm_join(actual)
    return claimed == truth or (len(claimed) >= 8 and claimed in truth)


def find_violations(claim_text, truth, *, strict=False,
                    coin_tolerance=0, hunger_value_tolerance=0):
    """Parse hunger/coin/location/last-message claims out of one agent status
    line and return a violation string for every claim that contradicts the
    ground truth. With strict=True a claim whose facet is absent from the
    ground truth is a violation too (a fact asserted without any grounding);
    with strict=False such claims are skipped, which is what the live checker
    wants when an endpoint is down."""
    text = _preprocess(claim_text)
    violations = []

    def add(message):
        if message not in violations:
            violations.append(message)

    for claim in extract_hunger_claims(text):
        if isinstance(claim, int):
            if truth.hunger_value is None:
                if strict:
                    add(f"hunger value: agent claimed {claim} but ground "
                        "truth carries no hunger value")
            elif abs(claim - truth.hunger_value) > hunger_value_tolerance:
                add(f"hunger value: agent claimed {claim} but the world "
                    f"reports {truth.hunger_value}")
        else:
            if truth.hunger_state is None:
                if strict:
                    add(f"hunger state: agent claimed '{claim}' but ground "
                        "truth carries no hunger state")
                continue
            world = _HUNGER_STATES.get(truth.hunger_state.lower(),
                                       truth.hunger_state.lower())
            if _HUNGER_STATES[claim] != world:
                add(f"hunger state: agent claimed '{claim}' but the world "
                    f"reports '{truth.hunger_state}'"
                    + (f" (value={truth.hunger_value})"
                       if truth.hunger_value is not None else ""))

    for item_phrase, count in extract_coin_claims(text):
        if truth.inventory is None:
            if strict:
                add(f"inventory: agent claimed {count} {item_phrase} but "
                    "ground truth carries no inventory")
            continue
        key = _resolve_item(item_phrase, truth.inventory)
        if key is None:
            add(f"inventory: agent claimed {count} {item_phrase} but the "
                f"inventory holds no such item "
                f"(has: {', '.join(sorted(truth.inventory)) or 'nothing'})")
        elif abs(count - truth.inventory[key]) > coin_tolerance:
            add(f"inventory: agent claimed {count} {item_phrase} but the "
                f"world reports {truth.inventory[key]} {key}")

    for place in extract_location_claims(text):
        if not truth.location_names:
            if strict:
                add(f"location: agent claimed '{place}' but ground truth "
                    "carries no location")
        elif not _location_matches(place, truth.location_names):
            add(f"location: agent claimed '{place}' but the world reports "
                f"spaceId '{truth.space_id or truth.location_names[0]}'")

    for quoted in extract_quote_claims(text):
        if truth.last_message is None:
            if strict:
                add(f'last message: agent claimed "{quoted}" but ground '
                    "truth carries no message record")
        elif not _quote_matches(quoted, truth.last_message):
            add(f'last message: agent claimed "{quoted}" but the actual '
                f'message was "{truth.last_message}"')

    return violations


# ---------------------------------------------------------------------------
# live checker
# ---------------------------------------------------------------------------

def _scrub(text):
    """Never let anything credential-shaped reach stdout."""
    return _BEARER_SCRUB_RE.sub("Bearer [REDACTED]",
                                _TOKEN_SCRUB_RE.sub("[REDACTED]", text))


def _say(message):
    print(_scrub(message), flush=True)


def _docker_logs(container, since, timeout, tail=None):
    """-> combined stdout+stderr of `docker logs`, or None with a message."""
    cmd = ["docker", "logs"]
    if since:
        cmd += ["--since", since]
    if tail:
        cmd += ["--tail", str(tail)]
    cmd.append(container)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=timeout)
    except FileNotFoundError:
        _say("docker CLI not found; cannot read the agent's send claims")
        return None
    except subprocess.TimeoutExpired:
        _say(f"docker logs timed out after {timeout:.0f}s")
        return None
    except OSError as error:
        _say(f"docker logs failed: {error}")
        return None
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        _say(f"docker logs failed for container '{container}': "
             f"{detail[-1][:200] if detail else 'unknown error'}")
        return None
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


_AGENT_LOG_RES = (
    re.compile(r"lease acquired: agent=([A-Za-z0-9._:-]+)"),
    re.compile(r"\bagent\.id:\s*([A-Za-z0-9._:-]+)"),
    re.compile(r"MCITY-STARTUP-OK[^\n]*?\bagent=([A-Za-z0-9._:-]+)"),
)


def _detect_agent_id(log_text):
    for pattern in _AGENT_LOG_RES:
        matches = pattern.findall(log_text or "")
        for candidate in reversed(matches):
            if candidate and candidate.lower() != "none":
                return candidate
    return None


def _fetch_json(url, timeout):
    """-> (payload | None, short error | None). Sends no credentials: the
    gateway injects the master token on the skill routes itself."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code}"
    except urllib.error.URLError as error:
        return None, str(getattr(error, "reason", error))
    except (TimeoutError, OSError) as error:
        return None, str(error)
    try:
        return json.loads(raw.decode("utf-8", errors="replace")), None
    except ValueError:
        return None, "response is not JSON"


def _fetch_ground_truth(gateway, agent_id, timeout):
    base = gateway.rstrip("/")
    payloads = {}
    for endpoint in ENDPOINTS:
        url = (f"{base}/mcity/api/skill/agents/"
               f"{urllib.parse.quote(agent_id, safe='')}/{endpoint}")
        payload, error = _fetch_json(url, timeout)
        payloads[endpoint] = payload
        if error is not None:
            _say(f"warning: {url} unavailable ({error}); "
                 "related checks are skipped")
    return payloads


def _truth_summary(truth):
    hunger = "unavailable"
    if truth.hunger_state is not None or truth.hunger_value is not None:
        hunger = (f"{truth.hunger_state or '?'}"
                  + (f" (value={truth.hunger_value})"
                     if truth.hunger_value is not None else ""))
    inventory = "unavailable"
    if truth.inventory is not None:
        inventory = " ".join(f"{key}={value}" for key, value
                             in sorted(truth.inventory.items())) or "(empty)"
    return (f"  hunger:    {hunger}\n"
            f"  location:  {truth.space_id or 'unavailable'}\n"
            f"  inventory: {inventory}")


_EPILOG = """\
exit codes:
  0  no contradiction between recent send claims and the world API
  1  at least one contradiction found
  2  gateway or container unavailable (nothing could be checked)

examples:
  python3 scripts/eval_grounding.py
  python3 scripts/eval_grounding.py --agent user-agent-ow0v8z9lg4v5kyr --since 1h
  MCITY_AGENT_ID=user-agent-... python3 scripts/eval_grounding.py --coin-tolerance 50

The world keeps moving while the log window ages: keep --since short, and
allow --coin-tolerance slack while the agent is actively trading."""


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="eval_grounding.py",
        description="Compare the OmegaClaw agent's recent `send` claims "
                    "(docker logs) against Midnight City ground truth "
                    "(local gateway). Manual tool; not wired into CI.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent",
                        default=os.environ.get("MCITY_AGENT_ID", ""),
                        help="Midnight City agent id (default: $MCITY_AGENT_ID"
                             " or auto-detected from the container log)")
    parser.add_argument("--gateway",
                        default=os.environ.get("MCITY_GATEWAY_URL",
                                               DEFAULT_GATEWAY_URL),
                        help=f"gateway base URL (default: %(default)s)")
    parser.add_argument("--container",
                        default=os.environ.get("OMEGACLAW_CONTAINER",
                                               DEFAULT_CONTAINER),
                        help="agent container name (default: %(default)s)")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help="docker logs window, e.g. 15m, 2h "
                             "(default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_HTTP_TIMEOUT,
                        help="per-request timeout in seconds "
                             "(default: %(default)s)")
    parser.add_argument("--max-claims", type=int, default=DEFAULT_MAX_CLAIMS,
                        help="check at most the N most recent send claims "
                             "(default: %(default)s)")
    parser.add_argument("--coin-tolerance", type=int, default=0,
                        help="allowed drift on claimed item counts "
                             "(default: %(default)s)")
    parser.add_argument("--hunger-tolerance", type=int, default=0,
                        help="allowed drift on a claimed hunger value "
                             "(default: %(default)s)")
    args = parser.parse_args(argv)

    log_timeout = max(15.0, args.timeout)
    log_text = _docker_logs(args.container, args.since, log_timeout)
    if log_text is None:
        _say("result: container log unavailable -> exit 2")
        return EXIT_UNAVAILABLE

    agent_id = args.agent.strip() or _detect_agent_id(log_text)
    if not agent_id:
        # The id lines appear at startup and inside skill results; the recent
        # window may hold neither, so fall back to a bounded full-log scan.
        older = _docker_logs(args.container, None, log_timeout, tail=100_000)
        agent_id = _detect_agent_id(older or "")
    if not agent_id or not _ID_RE.match(agent_id):
        _say("could not determine the agent id: pass --agent or set "
             "MCITY_AGENT_ID")
        _say("result: no agent id -> exit 2")
        return EXIT_UNAVAILABLE

    _say("== OmegaClaw Midnight City grounding check ==")
    _say(f"agent:     {agent_id}")
    _say(f"gateway:   {args.gateway.rstrip('/')}/mcity/api/skill/agents/"
         f"{agent_id}/{{{','.join(ENDPOINTS)}}}")
    _say(f"container: {args.container} (window: {args.since})")

    payloads = _fetch_ground_truth(args.gateway, agent_id, args.timeout)
    if all(payloads[endpoint] is None for endpoint in ENDPOINTS):
        _say("gateway unavailable: no ground truth could be fetched")
        _say("result: gateway unavailable -> exit 2")
        return EXIT_UNAVAILABLE

    truth = GroundTruth.from_api(needs=payloads["needs"],
                                 context=payloads["context"],
                                 inventory=payloads["inventory"])
    _say("ground truth:")
    _say(_truth_summary(truth))

    claims = extract_send_claims(log_text)[-max(1, args.max_claims):]
    if not claims:
        _say(f"no send claims found in the last {args.since}")
        _say("result: nothing to check -> exit 0")
        return EXIT_OK

    _say(f"claims ({len(claims)} send messages in window):")
    contradictions = 0
    for claim in claims:
        shown = claim if len(claim) <= 160 else claim[:157] + "..."
        violations = find_violations(
            claim, truth, strict=False,
            coin_tolerance=max(0, args.coin_tolerance),
            hunger_value_tolerance=max(0, args.hunger_tolerance))
        if violations:
            contradictions += 1
            _say(f'  CONTRADICTION "{shown}"')
            for violation in violations:
                _say(f"      - {violation}")
        else:
            _say(f'  ok            "{shown}"')

    if contradictions:
        _say(f"result: {contradictions} of {len(claims)} claims contradict "
             "the world -> exit 1")
        return EXIT_CONTRADICTION
    _say(f"result: all {len(claims)} claims consistent with the world "
         "-> exit 0")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
