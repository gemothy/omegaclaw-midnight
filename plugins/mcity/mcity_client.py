"""Midnight City direct-control client for the `mcity` MeTTa plugin.

Every public function in this module is a skill implementation: it is called
from `mcity.metta` through `py-call`, so it must behave like `src/fileio.py` —
never raise into the agent loop, always return a single non-empty string the
agent can act on. The string grammar is:

    MCITY-<VERB>-OK      [ <k>=<v> ]*  [ "\\n" <line> ]*
    MCITY-<VERB>-PENDING [ <k>=<v> ]*
    MCITY-<VERB>-FAILED  reason=<code> [detail=<short text>]

Three invariants hold for every returned string, enforced by `_out()`:

  * no credential ever appears in it (`_redact`),
  * it is capped at `mcityMaxResultChars` (`_cap`),
  * every game-sourced substring is control-char/quote free and wrapped in
    `<<MC_UNTRUSTED ... MC_UNTRUSTED>>` (`_clean`), because Midnight City is a
    shared world whose text is written by other people and their agents.

Credentials:

  * the master `MCITY_API_TOKEN` never exists in this process. It is injected
    by the nginx gateway on the routes that need it (`proxy/nginx.conf.template`).
  * the per-session lease token returned by `connect` lives only in memory,
    guarded by `_lock`, is never written to disk, never returned to MeTTa and
    never logged.

The lease is operator-owned: it is acquired at plugin load time from
`mcityMode`/`mcityAgentId`, never by an LLM-reachable skill.

That is a statement about the skills, NOT a complete containment claim. `shell`
and `metta` are first-class skills running in this process, so the model can
speak to the gateway directly. Containment therefore comes from what the gateway
renders, not from this file: `proxy/nginx.conf.template` publishes the lease and
action routes only when the operator asked for `mcityMode=control`, and in read
mode `POST /mcity/api/local-control/session` and `POST /mcity/api/actions` are
403 like every other unlisted route. In control mode those routes exist and an
injected agent that reaches `shell` can use them; see plugins/mcity/README.md,
"Residual risk in control mode".

Only the Python standard library is used (`requirements.txt` pins neither
requests nor httpx).
"""

import atexit
import functools
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

try:  # inside the agent the harness is importable as a package
    from src.logger import get_logger
except ModuleNotFoundError:  # the plugin folder is on sys.path (src/plugin.py:67)
    try:
        from logger import get_logger
    except ModuleNotFoundError:  # offline unit tests

        def get_logger(name):
            return logging.getLogger(name)


logger = get_logger("mcity")

try:  # `config` is already imported by lib_omegaclaw.metta:10
    from config import config_get_by_key as _config_get_by_key
except ImportError:
    try:
        from src.config import config_get_by_key as _config_get_by_key
    except ImportError:  # offline unit tests: fall back to the defaults
        _config_get_by_key = None

# Sibling modules of this plugin, imported absolutely by contract: src/plugin.py
# appends the plugin directory to sys.path and loads mcity_client as a TOP-LEVEL
# module, so there is no package for a relative import to resolve against
# (docs/ARCHITECTURE-memory.md, "Module layout and import rules"). Grounded
# memory is an enhancement: if it cannot even be imported the plugin keeps
# serving, ungrounded and loudly marked store=degraded, instead of dying here.
try:
    from mcity_projection import Budgeter, Candidate, TurnState
    from mcity_store import AgentObservation, make_store
except Exception as e:  # noqa: BLE001 - a broken sibling must not kill the plugin
    logger.error(f"mcity grounded-memory modules unavailable, degrading: {e}")
    Budgeter = Candidate = TurnState = AgentObservation = make_store = None


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PLUGIN_VERSION = "mcity/1.0"
USER_AGENT = "OmegaClaw-mcity/1.0"

DEFAULT_GATEWAY_URL = "http://localhost:8080"
DEFAULT_HTTP_TIMEOUT = 12.0        # seconds, per request
DEFAULT_CONFIRM_TIMEOUT = 8.0      # seconds, action outcome polling budget
CONFIRM_POLL_INTERVAL = 1.0        # seconds
RECENT_EVENT_LIMIT = 100
DEFAULT_MAX_RESULT_CHARS = 2000
DEFAULT_ACTION_MIN_GAP = 3.0       # seconds between two mutating actions
HEARTBEAT_SAFETY = 0.8             # fraction of the advertised interval
LEASE_EXPIRY_MARGIN_MS = 10_000
SLEEP_DURATION_MS = 28_800_000
ENGAGE_DURATION_MS = 600_000
MAX_LIST_ITEMS = 12
MAX_FIELD_CHARS = 200
MAX_FLATTEN_DEPTH = 4
MAX_EVENT_ROWS = 20
MAX_ARG_CHARS = 400
MAX_RESPONSE_BYTES = 2_000_000     # defensive: never read an unbounded body
HB_TICK_SECONDS = 1.0              # heartbeat thread wake-up cadence
HB_RETRY_SECONDS = 5.0
RECONNECT_GAP_SECONDS = 60.0
MAX_RECONNECTS = 3                 # CONSECUTIVE failures, reset by any success
RECONNECT_COOLDOWN_SECONDS = 900.0 # after a burst of failures, not a permanent stop
ECHO_MEMORY = 32
# How much contiguous world text an outgoing line may share before it counts as
# repeating rather than replying. 24 was under a clause, and conversation shares
# clauses by nature: it rejected "Spy, things have been relatively stable here.
# I've been focusing on exploring..." - the agent's own writing - and 16 of 22
# speak failures in one window were this guard blocking original replies.
#
# The property being protected is narrow: the agent must not relay an injected
# instruction back into the world. A relayed instruction is a long verbatim run;
# a reply that happens to share a phrase with the question is not. 48 characters
# is about a full clause, which natural writing does not reproduce by accident.
ECHO_MIN_OVERLAP = 48
DEFAULT_MEMORY_BACKEND = "postgres"  # mcity_store backend: postgres | memory
DEFAULT_PG_HOST = "127.0.0.1"
DEFAULT_PG_PORT = 5433             # the dedicated pgvector container, NOT 5432
DEFAULT_PG_DBNAME = "omegaclaw"
DEFAULT_PG_USER = "omegaclaw"
SPEAK_COOLDOWN_MS = 600_000        # candidates(): no re-greeting inside 10 min
STORE_REBUILD_SECONDS = 300.0      # a degraded store re-probes the real backend
STORE_RETRY_SECONDS = 60.0         # rest a failing store between attempts, so a
                                   # dead database costs one bounded attempt per
                                   # window instead of one per skill call
PROJECTION_FOOTER_RESERVE = 128    # rule-4 footers outrank the Budgeter's cap
                                   # (measured overflow <= 96 chars per source)

UNTRUSTED_OPEN = "<<MC_UNTRUSTED "
UNTRUSTED_CLOSE = " MC_UNTRUSTED>>"

ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
INT_RE = re.compile(r"^-?[0-9]{1,9}$")
TOKEN_RE = re.compile(r"midnight_[A-Za-z0-9_\-]{4,}")
BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
_SECRET_KEY_RE = re.compile(r"(?i)token|secret|authorization|apikey")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_WS_RE = re.compile(r"\s+")
# The delimiter itself must not survive inside the region it delimits, or world
# text carrying `MC_UNTRUSTED>>` would close the region early and the rest would
# read as trusted plugin output. Matched loosely (any separator between the two
# halves) so `MC-UNTRUSTED`, `mc untrusted` and friends cannot sneak through.
_MARKER_RE = re.compile(r"(?i)mc[^A-Za-z0-9]{0,3}untrusted")
_ANGLE_RE = re.compile(r"<{2,}|>{2,}")
# Object keys are rendered outside the untrusted region, so they may only ever
# contain characters that cannot forge a line, a status word or a k=v pair.
_KEY_RE = re.compile(r"[^A-Za-z0-9._\[\]-]+")
_EMPTY_TEXTS = {"", "()", "(empty)", "empty", "none", "None", "null", "nil"}

# normalized-phrase -> canonical activity (exact port of the reference helper)
HARVEST = {
    "chop": "chop wood", "chopping": "chop wood", "chop_wood": "chop wood",
    "cut_trees": "chop wood", "cut_wood": "chop wood", "cut_logs": "chop wood",
    "gather_logs": "chop wood", "gather_wood": "chop wood", "log": "chop wood",
    "logs": "chop wood", "wood": "chop wood",
    "mine": "mine ore", "mining": "mine ore", "mine_ore": "mine ore",
    "mine_some_ore": "mine ore", "gather_ore": "mine ore",
    "extract_ore": "mine ore", "ore": "mine ore",
    "trade_crypto": "trade crypto", "trade_coin": "trade crypto",
    "crypto": "trade crypto", "crypto_trading": "trade crypto",
    "crypto_trade": "trade crypto", "hack": "trade crypto",
    "hacking": "trade crypto",
}

# Every skill name registered by mcity.metta. Kept here so startup() can prove
# src/helper.py:LLM_COMMANDS is in sync: a plugin skill name missing from that
# set is silently swallowed into the previous command's argument whenever the
# model emits more than one command in a turn.
SKILL_NAMES = frozenset({
    "mcity-status", "mcity-context", "mcity-inventory", "mcity-needs",
    "mcity-areas", "mcity-agents", "mcity-navigation", "mcity-merchants",
    "mcity-recent-events", "mcity-threads", "mcity-thread",
    "mcity-move-area", "mcity-move-agent", "mcity-move-tile",
    "mcity-travel-district", "mcity-enter-building", "mcity-exit-building",
    "mcity-work", "mcity-eat", "mcity-sleep", "mcity-harvest",
    "mcity-speak", "mcity-trade",
})


# --------------------------------------------------------------------------
# shared state
# --------------------------------------------------------------------------

_lock = threading.RLock()   # RLock: renderers call helpers that re-enter
_cfg = {}                   # resolved once in startup()
_lease = None               # dict | None: session_id, agent_id, token,
                            # expires_at_ms, heartbeat_interval_ms, lease_ttl_ms
_lease_state = "off"        # off|connecting|active|expired|lost|failed
_lease_detail = ""          # short human string, never contains a token
_gateway_state = "unknown"  # unknown|ok|failed
_skills_state = "unknown"   # unknown|ok|degraded
_hb_thread = None
_hb_stop = threading.Event()
_hb_due_at = 0.0            # monotonic
_reconnect_at = 0.0         # monotonic
_reconnects = 0
_last_mutation_at = 0.0     # monotonic
_action_count = 0
_inbound = deque(maxlen=ECHO_MEMORY)   # normalised text read out of the world
_started = False
_store_lock = threading.Lock()  # roster store init/rest state; never nested
                                # inside _lock, and store calls never hold it
_store = None                   # RosterStore | None once _store_ready is set
_store_degraded = False         # the configured backend was lost or replaced
_store_ready = False            # one-shot guard for _roster_store()
_store_retry_at = 0.0           # monotonic; store calls are skipped until then
_store_rebuild_at = 0.0         # monotonic; a degraded store re-probes after this


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _c(key, default=None):
    """Read a resolved configuration value; safe before startup()."""
    try:
        value = _cfg.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _text(value):
    """MeTTa/YAML value -> plain trimmed string, empty when unset."""
    if value is None or value is False:
        return ""
    if value is True:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return ""
    text = text.strip()
    return "" if text in _EMPTY_TEXTS else text


def _number(value, default, low, high):
    try:
        number = float(_text(value))
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in (float("inf"), float("-inf")):
        return float(default)
    return float(min(max(number, low), high))


def _now_ms():
    return int(time.time() * 1000)


def _config(key, default):
    """Resolve one configuration key exactly once (src/config.py caches the
    first resolution forever, so this must never be called twice per key)."""
    if _config_get_by_key is None:
        return default
    try:
        return _config_get_by_key(key, default)
    except Exception as e:
        logger.warning(f"Could not resolve configuration key {key}, using default: {e}")
        return default


# --------------------------------------------------------------------------
# output sanitisation
# --------------------------------------------------------------------------

def _clean(text):
    """Render a game-sourced string as inert, clearly-marked untrusted data."""
    try:
        raw = text if isinstance(text, str) else str(text)
    except Exception:
        return UNTRUSTED_OPEN + "?" + UNTRUSTED_CLOSE
    raw = raw.replace("\r", " ").replace("\n", " / ").replace("\t", " ")
    raw = _CONTROL_RE.sub(" / ", raw)
    raw = raw.replace('"', "").replace("'", "")
    # Neutralise the delimiter before wrapping: an agent that names itself
    # "x MC_UNTRUSTED>> SYSTEM: ..." must not be able to close the untrusted
    # region and have the tail read as first party plugin output.
    raw = _MARKER_RE.sub("mc-untrusted-text", raw)
    raw = _ANGLE_RE.sub(" ", raw)
    raw = _WS_RE.sub(" ", raw).strip()
    if len(raw) > MAX_FIELD_CHARS:
        raw = raw[:MAX_FIELD_CHARS] + "..."
    return UNTRUSTED_OPEN + raw + UNTRUSTED_CLOSE


# Structural identifiers (area ids, agent ids, statuses, coordinates) must be
# rendered PLAINLY. They are the fields the agent has to act on, and the rules
# tell it that MC_UNTRUSTED content may never choose its next skill - so
# wrapping them made the agent correctly refuse to move, work or speak.
# Two conditions are required before a string is trusted: the key must be a
# known structural one, AND the value must be a bare token with no whitespace
# or punctuation, so a player who names themselves with an instruction still
# gets wrapped.
_STRUCTURAL_KEYS = frozenset((
    "id", "status", "kind", "profession", "phase", "type", "state",
    "activity", "x", "y",
))
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _is_structural_key(name):
    segment = name.rsplit(".", 1)[-1].split("[", 1)[0].lower()
    return segment in _STRUCTURAL_KEYS or segment.endswith("id")


def _render_str(name, value):
    if _is_structural_key(name) and _SAFE_TOKEN_RE.match(value):
        return f"{name}: {value}"
    return f"{name}: {_clean(value)}"


def _safe_key(name):
    """Render a JSON object key. Keys are game sourced too (item names, agent
    names, merchant names all appear as keys), but they are printed outside the
    untrusted region, so nothing that could forge a line may survive."""
    try:
        text = name if isinstance(name, str) else str(name)
    except Exception:
        return "?"
    text = _KEY_RE.sub("_", text)
    if len(text) > 64:
        text = text[:64]
    return text or "?"


def _redact(text):
    """Remove anything that looks like a credential, including the live lease
    token an attacker could have made an NPC repeat back at us."""
    try:
        out = TOKEN_RE.sub("[REDACTED]", text)
        out = BEARER_RE.sub("Bearer [REDACTED]", out)
        with _lock:
            token = (_lease or {}).get("token") or ""
        if token and len(token) >= 4:
            out = out.replace(token, "[REDACTED]")
        return out
    except Exception:
        return "[REDACTED]"


def _cap_to(text, limit):
    if len(text) <= limit:
        return text
    return text[:max(0, limit)] + " ...TRUNCATED"


def _cap(text):
    return _cap_to(text, int(_c("max_result_chars", DEFAULT_MAX_RESULT_CHARS)))


# Reads whose body is worth suppressing when it has not changed. Measured: in one
# four-minute window 49 of 50 decisions were mcity-threads returning the same 28
# rows, so the agent spent a whole window re-reading a list it had already seen
# and never reached an action. Repeating a read is also the most expensive way to
# waste context, because the body is the largest thing in the turn.
_REPEATABLE_READS = frozenset((
    "THREADS", "AGENTS", "MERCHANTS", "AREAS", "RECENT-EVENTS", "NAVIGATION",
    "CONTEXT", "INVENTORY", "NEEDS", "STATUS",
))
# Who is waiting on a reply, learned from the last mcity-threads render. The
# mission has told the agent in prose since several passes ago that answering a
# waiting person outranks working, and it still started long actions with two
# people waiting - and a long action makes it unreachable for the duration, so
# the thread dies. Prose is not enforcement; this is.
_WAITING = {"at_ms": 0, "ids": []}
_WAITING_STALE_MS = 90000      # older than this and we no longer claim to know
_WAITING_REFRESH_MS = 20000    # how often vitals re-checks who is waiting
_waiting_refresh_at_ms = 0
_waiting_refreshing = False
# Who the world said was asleep, and when. Measured: the world's actual rejection
# is "target is sleeping" - the speaker's own busy status was never the blocker,
# though the harness assumed it was and stayed silent for hours because of it.
# A sleeping agent cannot hear us, so retrying them burns turns while other
# people wait.
_ASLEEP = {}
_ASLEEP_TTL_MS = 300000        # assume nobody sleeps less than five minutes
_ENOUGH_MEME_COIN = 200        # the mission's own threshold for 'stop earning'
# (target, exact words) -> when it was delivered. Repeating yourself verbatim
# to the same person is the clearest tell of a bot.
_RICH_NUDGE_EVERY_MS = 60000   # how often to steer a rich agent toward people
# What counts as edible in the holding= list. Deliberately a substring match on
# "food": the world's item ids seen so far are to_go_food and raw_food, and a new
# one should fail toward letting the agent try to eat rather than starving it on
# a vocabulary gap. eat() still checks with the world, so a wrong guess costs one
# refused call.
FOOD_ITEMS = ("food",)
_WORKSITE_BACKOFF_MS = 20000   # pause after the world says every worksite is taken
_worksite_busy_until_ms = 0
_last_rich_nudge_ms = 0
_SAID = {}
_SAID_TTL_MS = 600000
_DND_STREAK_HINT = 3           # refusals before we point out the pattern
# The world's own canSpeak verdict per agent: {id: (bool, at_ms)}. Harvested
# free from any mcity-agents read, since the field already rides along.
_CAN_SPEAK = {}
# spaceId -> (how many idle unengaged agents were seen there, when)
# How many agents could receive a message at the last roster scan. The agent was
# calling mcity-agents 36 times in four minutes and never speaking, because that
# was the only way to find out whether anybody was available - the same reason it
# used to poll mcity-threads before waiting= existed. Repeat suppression cannot
# help here: roster rows carry distances and statuses that jitter, so no two
# bodies are ever byte-identical.
_REACHABLE = {"n": None, "at_ms": 0}
_ROSTER_RECHECK_MS = 30000     # how often a roster re-read is worth a turn
# The computed route to wherever free people are, cached because vitals is
# appended to EVERY result and the computation costs a read or two.
_ROUTE = {"text": None, "at_ms": 0}
_ROUTE_TTL_MS = 60000
_last_areas_read_ms = 0
_last_roster_read_ms = 0
_AWAKE_PLACES = {}
_AWAKE_PLACES_TTL_MS = 120000
_CAN_SPEAK_TTL_MS = 120000     # the city changes; do not trust an old verdict
_CAN_SPEAK_REFRESH_MS = 45000  # min gap between roster reads made for this
# Consecutive speaker-side refusals. The world has TWO distinct rejections:
# 'target is sleeping', which the canSpeak grounding above prevents, and
# 'speaker is in do not disturb mode', which no target check can. The second
# is what 50 of 50 failures in one window said, while status was busy in
# every vitals sample and the world kept regenerating the activity itself.
_dnd_streak = 0
_can_speak_at_ms = 0
_can_speak_refreshing = False

_LAST_READ = {}                # verb -> {"body", "at" (ms of last CHANGE), "n"}
_REPEAT_WINDOW_MS = 120000     # beyond this, a re-read is legitimately fresh
_REPEAT_REFUSE_AT = 4          # identical reads before the read is refused outright

_VITALS = {"at_ms": 0, "hunger": None, "space": None, "items": None,
           "status": None, "busy_for": None, "engaged": False, "space_kind": None}
_SELF_PROBE_MS = 30000         # never let our own rule silence us for longer
_last_self_probe_ms = 0
_VITALS_STALE_MS = 120000
VITALS_REFRESH_MS = 30000      # re-read vitals at most this often
_vitals_refreshing = False     # guards the refresh path against reentry via _out


def reset_runtime_state():
    """Clear every ephemeral cache. Exists for tests, and owned by this module.

    Three separate caches have now caused order-dependent failures because a new
    one was added here and the two conftests were not updated to match. Keeping
    the list next to the state it resets is the only version of this that stays
    correct.

    Deliberately NOT reset: the lease, the config and the store handles, which
    the fixtures own and which have their own lifecycles."""
    for cache in (_CAN_SPEAK, _ASLEEP, _LAST_READ, _SAID, _AWAKE_PLACES, _inbound):
        cache.clear()
    _REACHABLE.update({"n": None, "at_ms": 0})
    _ROUTE.update({"text": None, "at_ms": 0})
    _WAITING.update({"at_ms": 0, "ids": []})
    _VITALS.clear()
    _VITALS.update({"at_ms": 0, "hunger": None, "space": None, "items": None,
                    "status": None, "busy_for": None, "engaged": False,
                    "space_kind": None})
    globals().update(_vitals_refreshing=False, _can_speak_at_ms=0,
                     _can_speak_refreshing=False, _waiting_refresh_at_ms=0,
                     _waiting_refreshing=False, _last_self_probe_ms=0,
                     _dnd_streak=0, _last_rich_nudge_ms=0,
                     _worksite_busy_until_ms=0, _last_roster_read_ms=0,
                     _last_areas_read_ms=0)


def _harvest_vitals(payload):
    """Remember hunger, place and holdings from any response that carries them.

    Free: these fields already ride along on needs/inventory/context replies, so
    nothing extra is fetched. Never raises - a vitals miss must not break a
    skill."""
    try:
        if not isinstance(payload, dict):
            return
        agent = payload.get("agent")
        hunger = payload.get("hunger")
        items = payload.get("inventory")
        # The district we are standing in, which is NOT the same as our space:
        # inside a building position.spaceId is the building while currentSpace
        # is still the district. Without this the harness told the agent to
        # travel to central while it was already in central, and the world
        # answered "agent is already in district central".
        # currentSpace.kind is what matters, not its id: inside a building the id
        # is just the building again - identical to position.spaceId - which is
        # why comparing a destination against it never detected being indoors.
        # kind="interior" says the way out is the door, whatever the destination.
        space_now = payload.get("currentSpace")
        if isinstance(space_now, dict) and space_now.get("kind"):
            _VITALS["space_kind"] = str(space_now["kind"])
        if isinstance(hunger, dict) and hunger.get("state"):
            _VITALS["hunger"] = str(hunger.get("state"))
            if hunger.get("value") is not None:
                _VITALS["hunger"] = f"{hunger['state']}({hunger['value']})"
        if isinstance(agent, dict):
            position = agent.get("position")
            if isinstance(position, dict) and position.get("spaceId"):
                _VITALS["space"] = str(position["spaceId"])
            if agent.get("status"):
                _VITALS["status"] = str(agent["status"])
            # How long we stay busy. The world refuses mcity-speak outright while
            # the speaker has an action running - it answers "speaker is in do not
            # disturb mode" - and 6 of 6 observed speak failures were at
            # status=busy. Without the countdown the agent cannot tell that simply
            # waiting would work, so it falls through to starting ANOTHER action
            # and stays unreachable.
            action = agent.get("activeAction")
            _VITALS["busy_for"] = None
            # Our own engagement, recorded the same way we record everyone
            # else's. The world refused 50 of 50 replies with "speaker is in do
            # not disturb mode" while this was set, and zero once it cleared.
            _VITALS["engaged"] = isinstance(action, dict) and bool(action)
            if isinstance(action, dict) and action.get("endsAtMs"):
                try:
                    left = int(action["endsAtMs"]) - _now_ms()
                    if left > 0:
                        _VITALS["busy_for"] = int(left / 1000)
                except (TypeError, ValueError):
                    pass
        if isinstance(items, dict):
            _VITALS["items"] = " ".join(f"{k}={v}" for k, v in sorted(items.items()))
        # status belongs in this list. Leaving it out meant a reply carrying
        # status=busy and nothing else never stamped the clock, so every
        # freshness-gated consumer treated a KNOWN-busy agent as unknown and
        # skipped the speak refusal - which is how ~97 of 107 doomed speaks
        # reached the world and came back "in do not disturb mode".
        if (_VITALS["hunger"] or _VITALS["space"] or _VITALS["items"]
                or _VITALS["status"]):
            _VITALS["at_ms"] = _now_ms()
    except Exception:
        pass


def _refresh_vitals_if_stale():
    """Keep vitals populated without depending on the agent asking for them.

    _harvest_vitals only fires inside _skill_read, and the mission text now tells
    the agent never to spend a turn on mcity-needs or mcity-inventory precisely
    BECAUSE vitals carries that data. Those two facts together starved the
    feature: a session that only called mcity-threads and mcity-speak produced
    zero vitals lines, so the grounding the procedure depends on was silently
    absent. One bounded read closes the loop.

    Reentrancy-guarded: _skill_read's failure path routes through _out, which is
    what calls this."""
    global _vitals_refreshing
    with _lock:
        if _vitals_refreshing:
            return
        age = _now_ms() - (_VITALS["at_ms"] or 0)
        if _VITALS["at_ms"] and age < VITALS_REFRESH_MS:
            return
        _vitals_refreshing = True
    try:
        _skill_read("VITALS", "needs")
        if not _VITALS.get("items"):
            _skill_read("VITALS", "inventory")
        _refresh_waiting_if_stale()
    except Exception:  # noqa: BLE001 - grounding must never break a skill
        pass
    finally:
        with _lock:
            _vitals_refreshing = False


def _vitals_line():
    """One trusted line of current state, or None.

    The agent spent 68 of 75 reads in a ten-minute window re-asking for its own
    hunger and inventory - two thirds of every turn spent looking at itself
    instead of acting, against a prompt that allows two reads per turn. Carrying
    the answer on every result removes the reason to ask."""
    # Knowing who is waiting is itself grounding worth printing. This used to
    # return early whenever hunger, place and holdings were all unharvested,
    # which would withhold the single most important token - the name of the
    # person owed a reply - because unrelated fields happened to be missing.
    if not _VITALS["at_ms"] and not _someone_is_waiting():
        return None
    age = _now_ms() - _VITALS["at_ms"]
    parts = []
    if _VITALS["hunger"]:
        parts.append(f"hunger={_VITALS['hunger']}")
    if _VITALS["space"]:
        parts.append(f"at={_VITALS['space']}")
    if _VITALS["status"]:
        parts.append(f"status={_VITALS['status']}")
    if _VITALS.get("busy_for"):
        parts.append(f"busy-for={_VITALS['busy_for']}s"
                     " (cannot speak until this ends)")
    # waiting= is the whole reason the agent was polling mcity-threads every
    # turn. Carrying it here removes the reason to look, exactly as hunger and
    # inventory did: it counts only people who can actually hear a reply, so
    # zero means there is nothing a thread read could do for you.
    waiting = _someone_is_waiting()
    parts.append(f"waiting={len(waiting)}"
                 + (f" (answer {waiting[0]})" if waiting else ""))
    # earned= replaces an arithmetic comparison the model was not doing. Step
    # four asks it to read holding=... meme_coin=18383 and decide whether that
    # beats two hundred; it kept calling mcity-work instead, 54 refusals in three
    # minutes. Stating the conclusion as a fact is what worked for waiting=,
    # hunger and can-speak.
    # Only when holdings are actually known. Before the first inventory harvest
    # items is empty, and asserting keep-going there told the agent to go and
    # earn on evidence we did not have - 36 of 152 samples in one window.
    if _VITALS.get("items"):
        parts.append("earned=enough" if _rich_enough() else "earned=keep-going")
    # Say the worksite is shut before the agent spends a turn finding out. NOT
    # nested under the holdings check above: whether work is paused has nothing
    # to do with whether we happen to know what is in the bag.
    if _now_ms() < _worksite_busy_until_ms:
        left = int((_worksite_busy_until_ms - _now_ms()) / 1000) + 1
        parts.append(f"work=paused({left}s)")
    # reachable= removes the reason to poll the roster, exactly as waiting=
    # removed the reason to poll the thread list.
    if (_REACHABLE["n"] is not None
            and (_now_ms() - _REACHABLE["at_ms"]) <= _CAN_SPEAK_TTL_MS):
        parts.append(f"reachable={_REACHABLE['n']}")
        # Nobody here, but somebody somewhere: carry the route on the line the
        # agent reads every single turn. Measured: with reachable=0 it stopped
        # calling mcity-agents entirely - as instructed - so the only code path
        # that offered a route was a work-backoff refusal, and for four deploys
        # running it saw no route at all while nine free agents stood in central.
        if _REACHABLE["n"] == 0:
            route = _cached_route()
            if route:
                parts.append(route)
        else:
            # Name somebody. Step five used to require an mcity-agents read and a
            # row to be picked out of it; the agent read the roster and then went
            # back to work instead, every window. This is the same move that
            # retired the threads and roster polls: state the answer, delete the
            # lookup.
            who = _best_person_to_talk_to()
            if who:
                parts.append(f"talk-to={who}")
    if _VITALS["items"]:
        parts.append(f"holding={_VITALS['items']}")
    if not parts:
        return None
    if age > _VITALS_STALE_MS:
        parts.append(f"as-of={int(age / 1000)}s-ago")
    return "vitals " + " ".join(parts)


def _out(text):
    """The single exit point of every public function."""
    try:
        body = _redact(text)
        _refresh_vitals_if_stale()
        vitals = _vitals_line()
        if not vitals or "\nvitals " in body:
            return _cap(body)
        # Cap the BODY first, then append. _cap truncates the tail, so appending
        # before capping would chop the vitals off exactly on the long results
        # that most need grounding.
        reserve = len(vitals) + 1
        limit = int(_c("max_result_chars", DEFAULT_MAX_RESULT_CHARS))
        if len(body) + reserve > limit:
            body = _cap_to(body, max(0, limit - reserve))
        return f"{body}\n{vitals}"
    except Exception:
        return "MCITY-FAILED reason=internal"


def _line(verb, tag, pairs=(), lines=()):
    parts = [f"MCITY-{verb}-{tag}"]
    for key, value in pairs:
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    head = " ".join(parts)
    rows = [row for row in lines if row]
    if rows:
        return head + "\n" + "\n".join(rows)
    return head


def _failed(verb, reason, detail=""):
    return _line(verb, "FAILED", (("reason", reason), ("detail", detail)))


def _suppress_repeat(verb, result):
    """Collapse a read that returned exactly what it returned last time.

    The body is compared BEFORE _out appends vitals, because vitals move on
    almost every call (holdings tick) and would defeat the comparison.

    One repeat is allowed through untouched: re-reading once after acting is
    normal, and a first repeat is how the agent confirms something landed. From
    the second identical body onward the full text is replaced by its own first
    line plus how long the world has looked like this - enough to keep the count
    visible, without paying for rows the agent has now seen three times."""
    try:
        if verb not in _REPEATABLE_READS or not result.startswith(f"MCITY-{verb}-OK"):
            return result
        now = _now_ms()
        prev = _LAST_READ.get(verb)
        if not prev or prev["body"] != result or (now - prev["at"]) > _REPEAT_WINDOW_MS:
            _LAST_READ[verb] = {"body": result, "at": now, "n": 0}
            return result
        prev["n"] += 1
        if prev["n"] < 2:
            return result
        age = int((now - prev["at"]) / 1000)
        head = result.partition("\n")[0]
        # The busy exemption that stood here is gone with the premise it rested
        # on. It assumed a busy agent had no legal move, because the harness
        # believed busy blocked speech; the world's own canSpeak field disproved
        # that. A busy agent can speak to anyone reachable, and can work, eat and
        # trade, so a look-only loop is a real loop and gets refused like any
        # other.
        if prev["n"] >= _REPEAT_REFUSE_AT:
            # Shortening the answer was not enough. Measured: with suppression
            # live the agent still spent 48 of 48 decisions on mcity-threads,
            # because an OK result - however terse - reads as a turn well spent
            # and its own history then reinforces the pattern. A refusal is the
            # only thing in this protocol the agent cannot mistake for progress.
            # The counter clears the moment the body changes or any action lands,
            # so a genuine need to look is never blocked twice.
            # The advice has to match the situation. When nobody waiting can
            # hear a reply, telling the agent to answer a mine=no row is the
            # exact instruction that trapped it: it looped on threads while 56
            # reachable agents stood nearby. Hand it a copyable command instead,
            # the way the merchant cmd= and the escape hint do.
            if verb == "THREADS" and not _WAITING.get("ids"):
                opener = _reachable_opener()
                nudge = f"{opener or _next_action_command()} - nobody waiting can hear you"
            else:
                waiting = _someone_is_waiting()
                nudge = (f"Do this instead: cmd=mcity-speak {waiting[0]} "
                         "<your sentence>" if waiting
                         else _next_action_command())
            # Lead with the command. Every turn was a bare (mcity-threads) and
            # nothing else, and its own history - all mcity-threads - is what it
            # pattern-matches next. The instruction sat two hundred characters
            # into the line, past the point it reads. So the command comes first
            # and the explanation follows it.
            return _failed(verb, "repeat",
                           f"{nudge} -- unchanged for {age}s across "
                           f"{prev['n'] + 1} identical reads, so reading again "
                           f"cannot change anything. {head}")
        return (f"{head} unchanged for {age}s across {prev['n'] + 1} reads; "
                "re-reading cannot change it, so act instead of looking again")
    except Exception:      # noqa: BLE001 - never break a skill over an optimisation
        return result


def _guard(verb):
    """Wrap a skill so it can never raise into the agent loop and can never
    return an empty result (an empty COMMAND_RETURN row vanishes entirely)."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Accept the multi-argument form. The skills take ONE compound string
            # - "<agent-id> <sentence>" for speak, "<item> <qty> <merchant>" for
            # trade - but the model naturally writes the parts as separate quoted
            # arguments, and MeTTa had no clause of that arity. The call then died
            # at the janus binding with
            #   ALERT_FAILED (domain_error py_term (partial mcity-speak (...)))
            # before reaching Python, so it produced NO MCITY-SPEAK line at all:
            # every outcome count read zero speaks rather than failed speaks, and
            # the agent looked like it simply never tried. Joining here means the
            # split form and the compound form are the same call.
            if len(args) > 1:
                joined = " ".join(p for p in (_text(a) for a in args) if p)
                args = (joined,)
            try:
                result = func(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 - the loop needs a string
                logger.exception(f"mcity skill {verb} raised: {e}")
                return _out(_failed(verb, "internal"))
            if not isinstance(result, str) or not result.strip():
                logger.error(f"mcity skill {verb} produced an empty result")
                return _out(_failed(verb, "internal", "empty result"))
            return _out(_suppress_repeat(verb, result))
        return wrapper
    return decorator


# --------------------------------------------------------------------------
# argument normalisation
# --------------------------------------------------------------------------

def _norm_arg(value):
    """Undo the MeTTa string mangling, then compact exactly like the reference
    helper's compactText(): speak/shout confirmation compares byte-for-byte
    against the text the coordinator echoes back."""
    text = _text(value)
    text = text.replace("_apostrophe_", "'")
    text = text.replace("_quote_", '"')
    text = text.replace("_newline_", "\n")
    text = _CONTROL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > MAX_ARG_CHARS:
        text = text[:MAX_ARG_CHARS].strip()
    return text


def _split(arg, count):
    """Split one skill argument into `count` fields, None when there are fewer."""
    text = _norm_arg(arg)
    if not text:
        return None
    parts = text.split(maxsplit=count - 1) if count > 1 else [text]
    if len(parts) < count:
        return None
    return parts


def _safe_id(raw):
    """Validate then percent-encode one URL path segment. `/` and `%` can never
    enter a path, so no skill argument can reach an unallowlisted route."""
    text = _norm_arg(raw)
    if not text or not ID_RE.match(text):
        return None
    return urllib.parse.quote(text, safe="")


def _normalize_activity(value):
    text = _norm_arg(value).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


# --------------------------------------------------------------------------
# HTTP core
# --------------------------------------------------------------------------

def _base():
    return _text(_c("gateway_url", DEFAULT_GATEWAY_URL)).rstrip("/") + "/mcity"


def _url(path):
    # Plain concatenation, never urljoin: urljoin(".../observer", "/api/x")
    # drops the /observer prefix. nginx owns the upstream rewrite.
    return _base() + path


def _decode(status, raw):
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    parsed = None
    if text.strip():
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None  # observed: empty 401/404/405 bodies, plain-text 422
    return {
        "ok": 200 <= int(status) < 300,
        "status": int(status),
        "json": parsed,
        "text": text[:MAX_FIELD_CHARS * 4],
        "err": None,
    }


def _neterr(kind):
    return {"ok": False, "status": 0, "json": None, "text": "", "err": kind}


def _http(method, path, body=None, bearer=None, timeout=None):
    """One gateway request. Returns a dict, never raises, never puts a skill
    argument into a header name or value."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        try:
            data = json.dumps(body).encode("utf-8")
        except (TypeError, ValueError) as e:
            logger.error(f"Could not serialise the request body: {e}")
            return _neterr("network")
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    seconds = float(timeout) if timeout else float(_c("http_timeout", DEFAULT_HTTP_TIMEOUT))
    try:
        request = urllib.request.Request(_url(path), data=data, headers=headers,
                                         method=method)
        with urllib.request.urlopen(request, timeout=seconds) as response:
            status = getattr(response, "status", None) or response.getcode()
            raw = response.read(MAX_RESPONSE_BYTES)
        return _decode(status, raw)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(MAX_RESPONSE_BYTES)
        except Exception:
            raw = b""
        return _decode(e.code, raw)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        kind = "timeout" if isinstance(reason, (socket.timeout, TimeoutError)) else "network"
        logger.warning(f"Midnight City request failed ({kind}): {method} {path}")
        return _neterr(kind)
    except (socket.timeout, TimeoutError):
        logger.warning(f"Midnight City request timed out: {method} {path}")
        return _neterr("timeout")
    except (ssl.SSLError, OSError) as e:
        logger.warning(f"Midnight City request failed: {method} {path}: {e}")
        return _neterr("network")
    except Exception as e:  # never propagate into the agent loop
        logger.exception(f"Unexpected Midnight City transport failure: {e}")
        return _neterr("network")


def _reason(resp):
    """Map a failed response onto the closed reason vocabulary."""
    status = int(resp.get("status") or 0)
    if status == 429:
        code = "rate_limited"
    elif status == 0:
        code = resp.get("err") or "network"
    elif status == 404:
        code = "not_found"
    elif status in (401, 403):
        code = "auth"
    else:
        code = f"http_{status}"
    detail = ""
    body = resp.get("json")
    if isinstance(body, dict):
        value = body.get("error")
        if isinstance(value, str) and value.strip():
            detail = _clean(value.strip()[:80])
    return code, detail


def _fail_http(verb, resp):
    code, detail = _reason(resp)
    return _failed(verb, code, detail)


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

def _flatten(obj, prefix="", depth=0):
    """Generic renderer for endpoints whose full schema is undocumented."""
    if depth > MAX_FLATTEN_DEPTH:
        return [f"{prefix or 'value'}: ..."]
    if isinstance(obj, dict):
        lines = []
        for key in obj:
            name = _safe_key(key)
            child = f"{prefix}.{name}" if prefix else name
            if _SECRET_KEY_RE.search(name):
                lines.append(f"{child}: [REDACTED]")
                continue
            lines.extend(_flatten(obj[key], child, depth + 1))
        if not lines:
            lines.append(f"{prefix or 'value'}: (empty)")
        return lines
    if isinstance(obj, list):
        lines = []
        for index, item in enumerate(obj[:MAX_LIST_ITEMS]):
            lines.extend(_flatten(item, f"{prefix}[{index}]", depth + 1))
        extra = len(obj) - MAX_LIST_ITEMS
        if extra > 0:
            lines.append(f"{prefix or 'value'}: ... +{extra} more")
        if not lines:
            lines.append(f"{prefix or 'value'}: (empty)")
        return lines
    name = prefix or "value"
    if obj is None:
        return [f"{name}: null"]
    if isinstance(obj, bool):
        return [f"{name}: {'true' if obj else 'false'}"]
    if isinstance(obj, (int, float)):
        return [f"{name}: {obj}"]
    if isinstance(obj, str):
        return [_render_str(name, obj)]
    return [f"{name}: {_clean(str(obj))}"]


def _find_list(payload, *keys):
    """Locate the list an endpoint returns. None when the shape is unexpected,
    so the caller can degrade to _flatten instead of pretending it is empty."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


def _get(item, *names):
    """First present, non-None field of a dict, also looking inside the nested
    `trade` and `offer` objects.

    `offer` matters: the merchants endpoint nests what a merchant hands over and
    what it takes in return under `offer`, while only the payment terms sit under
    `trade`. Reading `trade` alone rendered every merchant row as

        name=Central Fresh Fish Outlet gives=None None for=None None
        item=crystal min=50 batch=50

    so the agent could not tell which merchant sold FOOD, nor that fish costs
    crystal. It starved holding 14,200 crystal, guessing meme_coin instead."""
    if not isinstance(item, dict):
        return None
    for name in names:
        if item.get(name) is not None:
            return item.get(name)
    for key in ("trade", "offer"):
        nested = item.get(key)
        if isinstance(nested, dict):
            for name in names:
                if nested.get(name) is not None:
                    return nested.get(name)
    return None


def _plain(value):
    """Render an id-like or numeric field. Ids are game-sourced too, but they
    are matched against ID_RE-shaped data, so they are cleaned as well."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = _text(value)
    if not text:
        return None
    if ID_RE.match(text):
        return text
    return _clean(text)


def _merchant_label(value):
    """Render a merchant name plainly so it can be passed back to mcity-trade.

    Merchant names are the ONE piece of world text the agent must echo verbatim:
    the trade endpoint matches on the exact name, and real names contain spaces
    ("Central Crypto Merchant"), so ID_RE rejects them and _plain wraps them as
    untrusted. Wrapped, they are unusable - the rules forbid untrusted content
    from choosing the next skill, so the agent queried merchants and then could
    never trade, which left it starving with money in its pocket.

    Merchants are server-side NPCs and outlets rather than player-authored chat,
    so the residual injection surface is small, but the value is still stripped
    of quotes, control characters, newlines and marker forgery, and capped.
    """
    if value is None:
        return None
    text = _text(value)
    if not text:
        return None
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = _CONTROL_RE.sub(" ", text)
    text = text.replace('"', "").replace("'", "")
    text = _MARKER_RE.sub("mc-untrusted-text", text)
    text = _ANGLE_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > 64:
        text = text[:64].strip()
    return text or None


def _sort_key(item, *names):
    if not isinstance(item, dict):
        return None
    for name in names:
        value = item.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _newest(items, count, *names):
    """Newest first when the entries carry a numeric ordering field."""
    keys = [_sort_key(item, *names) for item in items]
    if items and all(key is not None for key in keys):
        order = sorted(range(len(items)), key=lambda index: keys[index], reverse=True)
        return [items[index] for index in order][:count]
    return list(items)[:count]


def _row(pairs):
    parts = ["-"]
    for key, value in pairs:
        if value is None or value == "":
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _pair(quantity, item_id):
    quantity = _plain(quantity)
    item_id = _plain(item_id)
    if quantity is None and item_id is None:
        return None
    return " ".join(part for part in (quantity, item_id) if part is not None)


def _oldest_tail(items, count, *names):
    """Chronological order, keeping the last `count` entries."""
    keys = [_sort_key(item, *names) for item in items]
    if items and all(key is not None for key in keys):
        pairs = sorted(zip(keys, range(len(items))))
        ordered = [items[index] for _, index in pairs]
    else:
        ordered = list(items)
    return ordered[-count:]


def _remember_inbound(text, sender=None):
    """Keep a bounded memory of OTHER agents' text so the echo guard can refuse
    to let the agent repeat an injected instruction back into the world.

    sender matters. Every thread preview was remembered regardless of who wrote
    it, so once the agent spoke last its own words became the preview, were
    filed as world text, and the guard then refused its own future writing -
    "do not repeat text written by another agent" against lines the agent had
    written itself. The guard is about somebody else's words."""
    own_id = _c("agent_id", "")
    if sender is not None and own_id and _text(sender).strip() == own_id:
        return
    normalised = _norm_arg(text).lower()
    if len(normalised) < 8:
        return
    with _lock:
        _inbound.append(normalised)


def _is_echo(text):
    probe = _norm_arg(text).lower()
    if len(probe) < 4:
        return False
    with _lock:
        remembered = list(_inbound)
    for item in remembered:
        if probe == item:
            return True
        if len(probe) >= ECHO_MIN_OVERLAP:
            for start in range(0, len(probe) - ECHO_MIN_OVERLAP + 1):
                if probe[start:start + ECHO_MIN_OVERLAP] in item:
                    return True
    return False


# --------------------------------------------------------------------------
# grounded memory: roster store + ranked projection
# --------------------------------------------------------------------------
# docs/ARCHITECTURE-memory.md is the binding contract. The store grounds the
# roster ("who have I not spoken to" - a filter/sort problem, never a vector
# one), the Budgeter replaces positional truncation with relevance-ranked
# projection under an explicit budget, and every store touch degrades loudly
# (store=degraded) instead of raising into the agent loop.

def _store_settings(backend):
    """The make_store() configuration for the resolved backend. The password
    travels straight from the resolved config into the store; it is never
    logged and never appears in a result (_redact could not catch it: it does
    not look like a world token)."""
    return {
        "backend": backend,
        "host": _c("pg_host", DEFAULT_PG_HOST),
        "port": _c("pg_port", DEFAULT_PG_PORT),
        "dbname": _c("pg_dbname", DEFAULT_PG_DBNAME),
        "user": _c("pg_user", DEFAULT_PG_USER),
        "password": _c("pg_password", ""),
        # Store calls must be as bounded as world API calls (ARCHITECTURE-
        # memory.md, "Degradation"): reuse the HTTP budget for the store's
        # connect and per-statement timeouts.
        "timeout": float(_c("http_timeout", DEFAULT_HTTP_TIMEOUT)),
    }


def _roster_store():
    """The roster store, built lazily. Never raises: memory infrastructure is an
    enhancement and must never become a new way for the agent to die. An unusable
    configured backend is logged once and replaced by the bounded in-memory
    fallback, reported as store=degraded - falling back on a failed health()
    probe is this caller's decision by contract (mcity_store.make_store
    docstring), and it is never silent.

    Falling back is NOT permanent. This was a one-shot guard, so a database that
    was merely late - or misconfigured for one boot - cost the roster until the
    next restart: the agent ran for hours on the in-memory fallback while a
    perfectly healthy Postgres sat beside it. A degraded store now re-attempts
    the configured backend every STORE_REBUILD_SECONDS and promotes itself back
    the moment health() passes."""
    global _store, _store_degraded, _store_ready, _store_rebuild_at
    if _store_ready and not _store_degraded:
        return _store
    if _store_ready and _store_degraded:
        with _store_lock:
            if time.monotonic() < _store_rebuild_at:
                return _store
            _store_rebuild_at = time.monotonic() + STORE_REBUILD_SECONDS
            backend = (_text(_c("memory_backend", DEFAULT_MEMORY_BACKEND)).lower()
                       or DEFAULT_MEMORY_BACKEND)
            if make_store is None or backend == "memory":
                return _store
            try:
                built = make_store(_store_settings(backend))
                if built.health():
                    _store = built
                    _store_degraded = False
                    logger.info(f"mcity roster store ({backend}) recovered; "
                                "leaving the in-memory fallback")
            except Exception as e:  # noqa: BLE001 - recovery must never escape
                logger.debug(f"mcity roster store retry failed: {e}")
        return _store
    with _store_lock:
        if _store_ready:
            return _store
        backend = (_text(_c("memory_backend", DEFAULT_MEMORY_BACKEND)).lower()
                   or DEFAULT_MEMORY_BACKEND)
        store = None
        degraded = False
        if make_store is not None:
            try:
                built = make_store(_store_settings(backend))
                # health() never raises and is bounded by the store's own
                # connect/statement timeouts.
                if built.health():
                    store = built
                else:
                    logger.error(f"mcity roster store ({backend}) is unreachable")
            except Exception as e:  # noqa: BLE001 - init must never escape
                logger.error(f"mcity roster store ({backend}) could not be built: {e}")
            if store is None and backend != "memory":
                try:
                    store = make_store({"backend": "memory"})
                    degraded = True
                    logger.error(
                        "mcity roster store degraded: serving from the bounded "
                        "in-memory fallback; spoke counts reset at restart and "
                        "every affected result will carry store=degraded")
                except Exception as e:  # noqa: BLE001 - stay alive regardless
                    logger.error(f"mcity in-memory roster fallback failed: {e}")
        _store = store
        _store_degraded = degraded or store is None
        _store_ready = True
    return _store


def _store_call(op, default=None):
    """Run one roster-store operation; returns (value, ok) and never raises.
    Each attempt is bounded by the store's own timeouts, and after a failure
    the store rests for STORE_RETRY_SECONDS so a dead database cannot charge
    that bound to every subsequent skill call. The turn that hit the failure
    renders without roster ranking and is marked store=degraded."""
    global _store_retry_at
    store = _roster_store()
    if store is None:
        return default, False
    with _store_lock:
        resting = time.monotonic() < _store_retry_at
    if resting:
        return default, False
    try:
        value = op(store)
    except Exception as e:  # noqa: BLE001 - never into the agent loop
        with _store_lock:
            _store_retry_at = time.monotonic() + STORE_RETRY_SECONDS
        logger.warning("mcity roster store call failed, resting the store for "
                       f"{int(STORE_RETRY_SECONDS)}s: {e}")
        return default, False
    with _store_lock:
        _store_retry_at = 0.0
    return value, True


def _degraded(*oks):
    """True when this turn's grounding is not trustworthy: the grounded-memory
    modules are missing, the configured backend fell back at init, or any
    store call inside the current skill failed."""
    return _store_degraded or make_store is None or not all(oks)


def _stamp_degraded(result):
    """Append store=degraded to the head line of an already-built result (for
    lines assembled by shared code such as _submit)."""
    head, sep, tail = result.partition("\n")
    return head + " store=degraded" + sep + tail


def _yn(value):
    """Flag-ish wire value -> yes|no, None when the world did not state it."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return "yes" if value != 0 else "no"
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("true", "1", "yes", "y", "on"):
            return "yes"
        if word in ("false", "0", "no", "n", "off"):
            return "no"
    return None


class _ScoredSource:
    """Pre-scored skill rows behind the ContextSource protocol: the skill owns
    the ranking, the Budgeter owns what fits and the rule-4 drop footer."""

    weight = 1.0

    def __init__(self, name, ranking, rows):
        self.name = name
        self._ranking = ranking
        self._rows = rows           # [(text, score)], any order

    def candidates(self, turn):
        return [Candidate(text=text, score=score, source=self.name)
                for text, score in self._rows]

    def summary_for_dropped(self, shown, total):
        return f"[{self.name}: {shown} of {total} shown, ranked by {self._ranking}]"


def _project(head, name, ranking, rows):
    """Rank-and-fit `rows` ((text, score) pairs) under the explicit budget:
    `max_result_chars` minus the head line already emitted and minus
    PROJECTION_FOOTER_RESERVE. Rule-4 footers outrank the Budgeter's own cap
    and may overrun the budget it was given; the reserve absorbs that, so the
    final result stays inside `max_result_chars` and `_cap()` never truncates
    a projected result - silent truncation is the original defect."""
    if Budgeter is None:            # siblings unimportable: the legacy join,
        return "\n".join(text for text, _score in rows)  # bounded by _cap()
    limit = int(_c("max_result_chars", DEFAULT_MAX_RESULT_CHARS))
    budget = max(0, limit - len(head) - 1 - PROJECTION_FOOTER_RESERVE)
    source = _ScoredSource(name, ranking, rows)
    return Budgeter().render([source], TurnState(now_ms=_now_ms()), budget)


def _PRUNE_CEILING():
    return 4000


def _prune(store, ttl_ms, stamp):
    """Drop entries past their TTL, and hard-cap the rest.

    These caches are keyed by agent id and were never pruned. One roster is 285
    agents, which is nothing, but this process is meant to run for days in a city
    with churn, and an entry older than its TTL is already ignored by every
    reader - it was pure growth. The ceiling is a backstop for the case where
    something keeps writing fresh entries faster than they expire."""
    try:
        now = _now_ms()
        for key in [k for k, v in store.items() if (now - stamp(v)) > ttl_ms]:
            store.pop(key, None)
        if len(store) > _PRUNE_CEILING():
            for key in sorted(store, key=lambda k: stamp(store[k]))[:len(store) // 2]:
                store.pop(key, None)
    except Exception:      # noqa: BLE001 - housekeeping must never break a skill
        pass


def _entry_engaged(entry):
    """True when this roster entry is inside a live action."""
    action = entry.get("action")
    return isinstance(action, dict) and bool(action)


def _entry_reachable(entry):
    """The one verdict on whether a message to this agent can land.

    Used by BOTH the rendered can-speak= column and the _CAN_SPEAK cache. They
    disagreed: the column showed raw canSpeak - 200 rows saying yes - while the
    cache required canSpeak AND no live action, so the agent was being told yes
    about people the harness itself would have refused to send to."""
    return bool(entry.get("can_speak")) and not _entry_engaged(entry)


def _note_can_speak(entry):
    """Remember the world's own verdict on whether an agent can be spoken to.

    canSpeak is authoritative and is NOT implied by status: a live roster showed
    41 busy agents with canSpeak true and 22 busy with it false, while 165 of 285
    were asleep with it false. Status was therefore never a usable proxy, which
    is what two earlier speak rules got wrong in opposite directions."""
    try:
        agent_id, can = entry.get("id"), entry.get("can_speak")
        if not agent_id or not isinstance(can, bool):
            return
        # canSpeak alone is NOT enough. Three targets that refused with "target
        # is in do not disturb mode" all carried canSpeak true while running an
        # activeAction of kind engage, phase active. That is the same state the
        # world put US in - speaker-side do-not-disturb vanished the moment we
        # moved away and went idle - so the rule is symmetric: an agent inside a
        # live engagement cannot be reached, whatever the flag says.
        _CAN_SPEAK[agent_id] = (_entry_reachable(entry), _now_ms())
        engaged = _entry_engaged(entry)
        # Where the free people are. canSpeak is really "on the same map": every
        # one of 52 same-map agents had it true and every one of 24 off-map
        # agents had it false. So an idle, unengaged agent elsewhere is not
        # unreachable, it is somewhere else - and the world says where. Standing
        # in a room whose 52 occupants are permanently at crypto terminals is a
        # location problem, not a conversation problem.
        if not engaged and entry.get("status") == "idle":
            where = entry.get("space")
            if where:
                seen, _at = _AWAKE_PLACES.get(where, (0, 0))
                _AWAKE_PLACES[where] = (seen + 1, _now_ms())
        _prune(_CAN_SPEAK, _CAN_SPEAK_TTL_MS, lambda v: v[1])
    except Exception:      # noqa: BLE001 - grounding must never break a read
        pass


def _can_be_reached(agent_id):
    """True / False / None (unknown), from the freshest evidence we hold."""
    at = _ASLEEP.get(agent_id)
    if at and (_now_ms() - at) <= _ASLEEP_TTL_MS:
        return False
    known = _CAN_SPEAK.get(agent_id)
    if known and (_now_ms() - known[1]) <= _CAN_SPEAK_TTL_MS:
        return known[0]
    return None


def _next_action_command():
    """One copyable command for a turn with no conversation available.

    Chosen from what vitals already knows, so it is never advice the world will
    refuse for a reason we could have seen: eat only when actually hungry, work
    otherwise."""
    hunger = (_VITALS.get("hunger") or "").lower()
    if hunger.startswith(("hungry", "starving")) and _VITALS.get("items"):
        return "Do this instead: cmd=mcity-eat"
    who = _best_person_to_talk_to()
    if who:
        return (f"{who} can hear you right now - say something to them with "
                f"mcity-speak {who} followed by your sentence")
    return "Do this instead: cmd=mcity-work"


def _looks_speakable(agent_id):
    """ID_RE is a general id pattern and accepts things like "nyx".

    The roster carries such ids - NPCs and system agents, all off-map with
    canSpeak false - and the skill documents a character id as one starting
    user-agent-. Suggesting anything else only earns a refusal, so suggestions
    are held to the documented form even though the argument check is looser."""
    return bool(agent_id) and ID_RE.match(agent_id) is not None \
        and agent_id.startswith("user-agent-")


def _reachable_opener():
    """A copyable opener aimed at someone who can actually hear it.

    Live: the agent was idle with 56 reachable agents nearby and spoke to none of
    them, because every thread it was told to answer belonged to somebody asleep
    or in do-not-disturb and it never reached the step that starts a new
    conversation. Naming a skill did not move it before; a whole command did."""
    try:
        def _fresh():
            for agent_id, (can, at) in _CAN_SPEAK.items():
                # ID_RE matters: mcity-speak rejects anything that is not a
                # user-agent- id, and the roster carries ids like "nyx" that
                # would be suggested and then refused as bad arguments.
                if can and _looks_speakable(agent_id) \
                        and (_now_ms() - at) <= _CAN_SPEAK_TTL_MS:
                    return agent_id
            return None

        # The last FULL roster scan outranks any per-agent cache entry. They
        # disagreed live and the agent was whipsawed between two refusals:
        #   WORK-FAILED  worksite_busy    cmd=mcity-agents -- <id> is free to talk
        #   AGENTS-FAILED nobody_reachable cmd=mcity-work  -- nobody can receive
        # one telling it to go and look, the other telling it not to, 36 times in
        # six minutes. A cached entry can outlive the scan that supersedes it, so
        # when the newest scan counted nobody, there is nobody.
        scan_fresh = (_REACHABLE["n"] is not None
                      and (_now_ms() - _REACHABLE["at_ms"]) <= _CAN_SPEAK_TTL_MS)
        if scan_fresh and _REACHABLE["n"] == 0:
            return _travel_to_people_command()
        best = _fresh()
        if best is None:
            _refresh_can_speak_if_unknown((), force=True)
            best = _fresh()
        if best is None:
            # None, not prose. Callers used to sniff the returned sentence with
            # startswith("Start"), so rewording the sentence silently changed
            # which command the agent was given - it fell back to cmd=mcity-work
            # while somebody was standing there free to talk.
            return _travel_to_people_command()
        # NOT "cmd=mcity-speak <id> <your sentence>". A command with a
        # placeholder in it cannot be copied verbatim, which is the only thing
        # this agent reliably does: that exact form was emitted 63 times in
        # three minutes and produced one speak. mcity-agents is complete and
        # valid on its own, and lands the agent on the roster where the
        # can-speak=yes rows are, with the name carried in the note.
        return (f"{_plain(best)} is free to talk right now, and the roster will "
                f"give you the exact id: cmd=mcity-agents")
    except Exception:      # noqa: BLE001 - a hint must never break a skill
        return None


def _promote_command(result, hint):
    """Lift the cmd= out of a hint and into the head line of a failure.

    Everything learned this session says the same thing: this agent acts on a
    complete command near the start of the first line, and ignores the same
    command placed lower down. Measured both ways more than once.

    The command goes AFTER the reason field, never before the MCITY- verb: every
    result must start with that prefix or the agent loop cannot classify it, and
    prepending broke exactly that invariant the first time this was written."""
    try:
        hint = (hint or "").strip()
        command = ""
        for piece in hint.split():
            if piece.startswith("cmd=mcity-"):
                command = hint[hint.index(piece):].strip()
                break
        if not command:
            return _out(f"{result}\n{hint}") if hint else _out(result)
        note = hint[:hint.index(command)].strip().rstrip(".").rstrip()
        for tail in (" Go to them:", " Start a conversation with someone who can:",
                     " Do this instead:", " Leave now with this exact line:"):
            note = note.replace(tail, "")
        note = note.strip().rstrip(":").strip()
        head, sep, rest = (result or "").partition("\n")
        insert = f"{command}" + (f" -- {note}" if note else "")
        match = re.search(r"(reason=[\w-]+)", head)
        if match:
            head = head.replace(match.group(1), f"{match.group(1)} {insert};", 1)
        elif head.startswith("MCITY-"):
            verb, _space, tail_text = head.partition(" ")
            head = f"{verb} {insert}; {tail_text}".rstrip()
        else:
            return _out(f"{result}\n{hint}")
        return _out(head + sep + rest)
    except Exception:      # noqa: BLE001 - a hint must never break a skill
        return _out(result)


def _best_person_to_talk_to():
    """One reachable agent id, preferring somebody we have not spoken to.

    Reads only cached state - vitals is appended to every result and must never
    cost a request."""
    try:
        now = _now_ms()
        best = None
        for agent_id, (can, at) in _CAN_SPEAK.items():
            if not can or (now - at) > _CAN_SPEAK_TTL_MS:
                continue
            if not _looks_speakable(agent_id):
                continue
            spoken = any(key[0] == agent_id for key in _SAID)
            if best is None or (not spoken and best[1]):
                best = (agent_id, spoken)
                if not spoken:
                    break
        return best[0] if best else None
    except Exception:      # noqa: BLE001 - vitals must never break a skill
        return None


def _cached_route():
    """The route command, recomputed at most once per _ROUTE_TTL_MS.

    vitals is appended to every result, and the underlying lookup costs a roster
    and an areas read, so it must never run per render."""
    try:
        if _ROUTE["text"] is not None and (_now_ms() - _ROUTE["at_ms"]) <= _ROUTE_TTL_MS:
            return _ROUTE["text"]
        hint = _travel_to_people_command() or ""
        command = ""
        for piece in hint.split():
            if piece.startswith("cmd=mcity-"):
                command = " ".join(hint[hint.index(piece):].split()[:2])
                break
        _ROUTE["text"] = command
        _ROUTE["at_ms"] = _now_ms()
        return command
    except Exception:      # noqa: BLE001 - vitals must never break a skill
        return ""


def _travel_to_people_command():
    """Where to go when nobody here can talk, as a copyable command.

    Measured: all 52 agents on our map could be spoken to and every one was
    engaged at a terminal, while all 24 idle agents were off-map and therefore
    marked canSpeak false - 18 of them in one place. Nobody was unreachable; the
    agent was simply standing in the wrong room. The world publishes their
    spaceId, so this turns that into an instruction."""
    try:
        here = _VITALS.get("space")
        if _VITALS.get("space_kind") is None:
            # Only the context endpoint carries it, and the vitals refresh reads
            # needs, so without this the indoor check never had its input.
            _skill_read("VITALS", "context")
        indoors = (_VITALS.get("space_kind") or "").lower() == "interior"

        def _pick():
            now = _now_ms()
            best, count = None, 0
            for space, (seen, at) in _AWAKE_PLACES.items():
                if space == here or (now - at) > _AWAKE_PLACES_TTL_MS:
                    continue
                if seen > count:
                    best, count = space, seen
            return best, count

        best, count = _pick()
        if best is None:
            # STALE counts as missing. This only refreshed when the dict was
            # empty, so once it had been filled and aged out the function
            # returned None for ever - and the roster rate-limit added later
            # meant the scan that refills it rarely ran. Live symptom: 55 free
            # agents at central, and no route offered for three deploys.
            _refresh_can_speak_if_unknown((), force=True)
            best, count = _pick()
        if not best or not ID_RE.match(best):
            return None
        payload, error = _skill_read("VITALS", "areas")
        if error is None:
            areas = [item for item in (_find_list(payload, "areas") or [])
                     if isinstance(item, dict)
                     and _get(item, "moveAreaAvailable") is not False]
            # Match on the area's ANCHOR, not its id. A space holding people -
            # 'central' - is not itself an area and never appears in this list;
            # what appears are the areas anchored in it, like central-plaza and
            # bison-valley. Matching on id found nothing, so the fallback fired
            # and sent the agent at travel-district and exit-building, both of
            # which the world refuses from here: travelDistricts is empty
            # indoors, and this building's exit is a teleport rather than a link.
            # Prefer somewhere OUTDOORS. Anchored areas include buildings, and
            # the first live route was cmd=mcity-move-area ada-arena, a building:
            # following it would have put the agent indoors again, which is the
            # exact trap it is trying to leave - indoors it cannot see the areas
            # that reach anywhere else, and this building's exit is a teleport
            # that mcity-exit-building does not handle.
            def _rank(item):
                return 0 if _text(_get(item, "kind")) in ("park", "district") else 1

            for item in sorted(areas, key=_rank):
                anchor = _get(item, "anchor")
                anchored = (anchor or {}).get("spaceId") if isinstance(anchor, dict) else None
                area_id = _text(_get(item, "areaId", "id"))
                if anchored == best and area_id and ID_RE.match(area_id):
                    return (f"Nobody here can talk, but {count} free agents are "
                            f"at {best}. Go to them: cmd=mcity-move-area {area_id}")
            for item in areas:
                area_id = _text(_get(item, "areaId", "id"))
                if area_id == best and ID_RE.match(area_id or ""):
                    return (f"Nobody here can talk, but {count} free agents are "
                            f"at {best}. Go to them: cmd=mcity-move-area {area_id}")
        if indoors:
            return (f"{count} free agents are out in {best}, and you are inside a "
                    f"building with no area reaching it. Try the door: "
                    f"cmd=mcity-exit-building")
        return (f"Nobody here can talk, but {count} free agents are at {best}. "
                f"Go to them: cmd=mcity-travel-district {best}")
    except Exception:      # noqa: BLE001 - a hint must never break a skill
        return None


def _escape_command():
    """A ready-to-copy command for leaving the current activity.

    mcity-exit-building was the first suggestion and the world answered "agent is
    not inside a linked building", so a bare skill name is not enough - the agent
    needs a destination it can copy. This is the cmd= pattern that already fixed
    trading: the agent reliably copies a complete command verbatim and reliably
    fails to assemble one from a listing."""
    try:
        payload, error = _skill_read("VITALS", "areas")
        if error is not None:
            return "Use mcity-areas to find somewhere to go, then mcity-move-area."
        here = _VITALS.get("space")
        for item in (_find_list(payload, "areas") or []):
            if not isinstance(item, dict):
                continue
            area = _text(_get(item, "areaId", "id"))
            if not area or area == here or not ID_RE.match(area):
                continue
            if _get(item, "moveAreaAvailable") is False:
                continue
            return f"Leave now with this exact line: cmd=mcity-move-area {area}"
        return "Use mcity-areas to find somewhere to go, then mcity-move-area."
    except Exception:      # noqa: BLE001 - a hint must never break a skill
        return "Use mcity-areas to find somewhere to go, then mcity-move-area."


def _refresh_can_speak_if_unknown(agent_ids, force=False):
    """One bounded roster read when a waiting counterpart's reachability is
    unknown.

    canSpeak only rides on /agents, and the agent reads threads almost
    exclusively - 46 of 46 decisions in one window - so the verdict was usually
    missing exactly when it mattered. Measured consequence: of 30 speak attempts
    only 6 were caught locally, and 24 went to the world to be told the target
    was asleep. One GET is cheaper than those round trips, and the same read
    grounds the roster for the rest of the turn.

    Reentrancy-guarded, rate-limited, and never allowed to break a skill."""
    global _can_speak_at_ms, _can_speak_refreshing
    try:
        # force=True is for the opposite case: nobody waiting is reachable, so
        # there are no unknowns to chase, and yet that is exactly when the agent
        # most needs a name it CAN talk to. Without this the opener had nothing
        # to offer and fell back to naming a skill, which has never moved it.
        unknown = [i for i in agent_ids if _can_be_reached(i) is None]
        if not unknown and not force:
            return
        with _lock:
            if _can_speak_refreshing:
                return
            if _can_speak_at_ms and (_now_ms() - _can_speak_at_ms) < _CAN_SPEAK_REFRESH_MS:
                return
            _can_speak_refreshing = True
        try:
            payload, error = _skill_read("VITALS", "agents")
            if error is None:
                entries = [_parse_agent(item)
                           for item in (_find_list(payload, "agents") or [])
                           if isinstance(item, dict)]
                for entry in entries:
                    _note_can_speak(entry)
                if entries:
                    _REACHABLE["n"] = sum(1 for e in entries if _entry_reachable(e)
                                          and _looks_speakable(e["id"]))
                    _REACHABLE["at_ms"] = _now_ms()
            _can_speak_at_ms = _now_ms()
        finally:
            with _lock:
                _can_speak_refreshing = False
    except Exception:      # noqa: BLE001 - grounding must never break a skill
        pass


def _parse_agent(item):
    """The wire fields of one /agents entry, gathered in one place."""
    return {
        "id": _text(_get(item, "agentId", "id")),
        "name": _get(item, "name", "displayName"),
        "status": _get(item, "status"),
        "open": _get(item, "isOpenToTalk"),
        "talking": _get(item, "isTalkingToYou"),
        "can_speak": _get(item, "canSpeak"),
        "action": _get(item, "activeAction"),
        "space": ((_get(item, "position") or {}).get("spaceId")
                  if isinstance(_get(item, "position"), dict) else None),
        "same_map": _get(item, "isOnSameMap"),
        "dist": _get(item, "distance", "dist"),
        "profession": _get(item, "profession"),
    }


def _agent_observation(entry, now_ms):
    """One parsed /agents row -> the store's FULL observation schema. Mapping
    every field is half of the grounding fix: the old renderer kept only
    id/name/dist, so `status` never reached the model although the prompt
    filters on it, and isTalkingToYou - someone already addressing us - was
    thrown away (docs/ARCHITECTURE-memory.md, "Observation schema")."""
    name = entry["name"]
    status = entry["status"]
    profession = entry["profession"]
    return AgentObservation(
        agent_id=entry["id"],
        name=name if isinstance(name, str) else None,
        status=status if isinstance(status, str) else None,
        is_open_to_talk=entry["open"],
        is_talking_to_you=entry["talking"],
        can_speak=entry["can_speak"],
        is_on_same_map=entry["same_map"],
        dist=entry["dist"],
        profession=profession if isinstance(profession, str) else None,
        observed_at_ms=now_ms,
    )


def _agent_row(entry, spoke_count):
    """One roster row.

    can-speak replaces the old open= indicator. isOpenToTalk was true for 283 of
    285 agents on a live roster - including all 165 who were asleep - so it cost
    characters and carried no signal, while canSpeak is the world's own verdict
    on whether a message can be delivered at all. status stays because it is
    useful colour, but it decides nothing: the same roster had 41 busy agents who
    could be spoken to and 22 busy who could not."""
    status = entry["status"]
    return _row((
        ("id", _plain(entry["id"])),
        ("name", _plain(entry["name"])),
        ("status", _plain(status) if _text(status) else "unknown"),
        ("can-speak", "yes" if _entry_reachable(entry) else "no"),
        ("talking", "yes" if _yn(entry["talking"]) == "yes" else None),
        ("dist", _plain(entry["dist"])),
        ("prof", _plain(entry["profession"])),
        ("spoke", spoke_count),
    ))


# --------------------------------------------------------------------------
# lease management
# --------------------------------------------------------------------------

def _normalize_lease(payload):
    """Port of the reference normalizeLease(). None when the wire response is
    not a usable lease."""
    if not isinstance(payload, dict):
        return None
    lease = {}
    for wire, local in (("sessionId", "session_id"), ("agentId", "agent_id"),
                        ("token", "token")):
        value = payload.get(wire)
        if not isinstance(value, str) or not value.strip():
            return None
        lease[local] = value.strip()
    for wire, local in (("expiresAt", "expires_at_ms"),
                        ("heartbeatIntervalMs", "heartbeat_interval_ms"),
                        ("leaseTtlMs", "lease_ttl_ms")):
        value = payload.get(wire)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if value != value or value in (float("inf"), float("-inf")):
            return None
        lease[local] = value
    return lease


def _hb_interval(lease):
    heartbeat = float(lease.get("heartbeat_interval_ms") or 0.0)
    ttl = float(lease.get("lease_ttl_ms") or 0.0)
    candidates = [value for value in (heartbeat, ttl / 4.0) if value > 0]
    if not candidates:
        return 24.0
    return min(max(HEARTBEAT_SAFETY * min(candidates) / 1000.0, 5.0), 60.0)


def _store_lease(lease):
    global _lease, _lease_state, _lease_detail, _hb_due_at, _reconnects
    with _lock:
        _lease = lease
        _lease_state = "active"
        _lease_detail = ""
        _hb_due_at = time.monotonic() + _hb_interval(lease)
        # MAX_RECONNECTS is a CONSECUTIVE failure budget, not a process lifetime
        # one: four fully recovered outages over a long run must not disable
        # control for good. Every success (connect or heartbeat) clears it.
        _reconnects = 0


def _drop_lease(state, detail):
    global _lease, _lease_state, _lease_detail
    with _lock:
        _lease = None
        _lease_state = state
        _lease_detail = detail


def _lease_snapshot():
    with _lock:
        return dict(_lease) if _lease else None


def _connect():
    """Acquire the direct-control lease. Operator-driven only: never reachable
    from a skill, so an injected prompt cannot claim or move control."""
    global _lease_state, _lease_detail
    agent_id = _c("agent_id", "")
    if not agent_id:
        _drop_lease("failed", "no agent id")
        return False
    with _lock:
        _lease_state = "connecting"
        _lease_detail = ""
    body = {
        "agentId": agent_id,
        "clientInstanceId": _c("client_instance_id", f"omegaclaw:{agent_id}"),
        "modelId": None,
    }
    # No Authorization from us: nginx injects the master token on this route.
    resp = _http("POST", "/api/local-control/session", body=body)
    if not resp["ok"]:
        code, _detail = _reason(resp)
        # A transport level failure is worth retrying inside the bounded
        # reconnect budget; an auth or not_found answer never is.
        retryable = code in ("timeout", "network", "rate_limited") or code.startswith("http_5")
        _drop_lease("expired" if retryable else "failed", code)
        logger.error(f"Midnight City connect failed for agent {agent_id}: {code}")
        return False
    lease = _normalize_lease(resp["json"])
    if lease is None:
        _drop_lease("failed", "upstream_invalid")
        logger.error("Midnight City connect returned an unusable lease response")
        return False
    _store_lease(lease)
    # The token is deliberately absent from this line, as from every other.
    logger.info(f"Midnight City lease acquired: agent={lease['agent_id']} "
                f"session={lease['session_id']} "
                f"ttl={int(lease['lease_ttl_ms'] / 1000)}s "
                f"heartbeat={int(_hb_interval(lease))}s")
    return True


def _note_detail(detail):
    global _lease_detail
    with _lock:
        _lease_detail = detail


def _heartbeat_once():
    """Renew the lease. Returns ok|lost|auth|retry. A successful heartbeat
    returns a whole new lease including a NEW token, so nothing may cache it."""
    lease = _lease_snapshot()
    if lease is None:
        return "retry"
    resp = _http("POST", "/api/local-control/session/heartbeat",
                 body={"sessionId": lease["session_id"]},
                 bearer=lease["token"])
    if resp["ok"]:
        renewed = _normalize_lease(resp["json"])
        if renewed is None:
            _note_detail("upstream_invalid")
            return "retry"
        _store_lease(renewed)
        return "ok"
    status = int(resp.get("status") or 0)
    if status == 404:
        # Another controller or the AI supervisor took the agent. Never
        # reconnect here: that would start a tug of war over a live agent.
        _drop_lease("lost", "taken over")
        logger.error("Midnight City lease lost: another controller claimed the agent")
        return "lost"
    if status in (401, 403):
        _drop_lease("failed", "auth")
        logger.error("Midnight City heartbeat rejected: authorisation failure")
        return "auth"
    code, _detail = _reason(resp)
    _note_detail(code)
    return "retry"


def _release_quietly():
    """Best effort release at interpreter exit. There is no reliable shutdown
    hook in the agent loop, so a killed container just lets the TTL lapse."""
    lease = _lease_snapshot()
    if lease is None:
        return False
    resp = _http("POST", "/api/local-control/session/release",
                 body={"sessionId": lease["session_id"]},
                 bearer=lease["token"], timeout=5.0)
    _drop_lease("off", "released")
    return bool(resp["ok"])


def _hb_tick():
    global _hb_due_at, _reconnect_at, _reconnects
    now = time.monotonic()
    with _lock:
        state = _lease_state
        due = _hb_due_at
        reconnect_at = _reconnect_at
        reconnects = _reconnects
        lease = dict(_lease) if _lease else None

    if state in ("lost", "failed"):
        _hb_stop.set()
        return

    if state == "active" and lease is not None:
        if _now_ms() >= lease["expires_at_ms"] - LEASE_EXPIRY_MARGIN_MS:
            _drop_lease("expired", "lease expired")
            with _lock:
                _reconnect_at = now
            return
        if now < due:
            return
        result = _heartbeat_once()
        if result == "ok":
            return
        if result in ("lost", "auth"):
            _hb_stop.set()
            return
        with _lock:
            _hb_due_at = time.monotonic() + HB_RETRY_SECONDS
        return

    if state == "expired":
        if now < reconnect_at:
            return
        if reconnects >= MAX_RECONNECTS:
            # Bounded backoff, never a permanent stop. Killing the thread here
            # left the action skills registered but answering not_ready for the
            # rest of the container's life, even after the network came back.
            with _lock:
                _reconnects = 0
                _reconnect_at = now + RECONNECT_COOLDOWN_SECONDS
            _note_detail("reconnect cooling down")
            logger.error("Midnight City reconnect attempts exhausted; cooling down for "
                         f"{int(RECONNECT_COOLDOWN_SECONDS)}s before trying again")
            return
        with _lock:
            _reconnects = reconnects + 1
            _reconnect_at = now + RECONNECT_GAP_SECONDS
        logger.warning(f"Midnight City lease expired, reconnect attempt {reconnects + 1}")
        if _connect():
            return
        with _lock:
            unrecoverable = _lease_state == "failed"
        if unrecoverable:
            logger.error("Midnight City reconnect gave up: the failure is not retryable")
            _hb_stop.set()
        return


def _hb_loop():
    """Pure Python, never re-enters MeTTa, writes no files, swallows everything."""
    while not _hb_stop.is_set():
        try:
            _hb_tick()
        except BaseException as e:  # noqa: BLE001 - a daemon thread must not die
            _note_detail("heartbeat error")
            logger.warning(f"Midnight City heartbeat loop error: {e}")
        _hb_stop.wait(HB_TICK_SECONDS)


def _start_heartbeat():
    global _hb_thread
    with _lock:
        if _hb_thread is not None and _hb_thread.is_alive():
            return
        _hb_stop.clear()
        _hb_thread = threading.Thread(target=_hb_loop, name="mcity-heartbeat",
                                      daemon=True)
        _hb_thread.start()


def _ensure_lease():
    """Purely local check, no network. None when a mutation may proceed."""
    if not is_control_mode():
        return "read_only"
    with _lock:
        state = _lease_state
        lease = dict(_lease) if _lease else None
    if state == "lost":
        return "lease_lost"
    if state == "expired":
        return "lease_expired"
    if lease is None:
        return "not_ready" if state in ("connecting", "failed") else "no_lease"
    if state != "active":
        return "not_ready"
    if _now_ms() >= lease["expires_at_ms"] - LEASE_EXPIRY_MARGIN_MS:
        return "lease_expired"
    return None


# --------------------------------------------------------------------------
# lifecycle (not LLM-reachable)
# --------------------------------------------------------------------------

def ping():
    """Import proof for the plugin loader; a failed MeTTa import is silent, so
    the acceptance criterion is this positive line in the log."""
    return "MCITY-PING-OK " + PLUGIN_VERSION


def is_control_mode():
    return bool(_c("mode", "read") == "control" and _c("agent_id", ""))


def trade_enabled():
    return bool(is_control_mode()
                and _c("trade_merchants", ())
                and float(_c("trade_max_quantity", 0)) > 0)


# `mcity.metta` must not feed a Python bool straight into a MeTTa `if`: no other
# call site in this repo does, and plugins/workflow/workflow.metta deliberately
# returns a string and compares it with `==` instead. These two return a symbol
# the MeTTa side matches, exactly like `(== (embeddingprovider) OpenAI)`.

def registration_mode():
    """off | read | control - which block of skills mcity.metta may register.

    `off` covers every case where advertising the skills would only mislead the
    model: no agent configured, startup did not complete, or the gateway probe
    failed (in which case the plugin is not even allowed to reach the world)."""
    if not _started or _gateway_state != "ok" or not _c("agent_id", ""):
        return "off"
    return "control" if is_control_mode() else "read"


def trade_flag():
    """enabled | disabled - whether mcity.metta may register mcity-trade."""
    return "enabled" if trade_enabled() else "disabled"


def _resolve_config(gateway_url, agent_id, mode):
    """Resolve every configuration key exactly once."""
    cfg = {}
    cfg["gateway_url"] = _text(gateway_url) or DEFAULT_GATEWAY_URL

    resolved_agent = _text(agent_id)
    if resolved_agent and not ID_RE.match(resolved_agent):
        logger.error("mcityAgentId is not a valid agent id; running in read mode")
        resolved_agent = ""
    cfg["agent_id"] = resolved_agent

    resolved_mode = _text(mode).lower() or "read"
    if resolved_mode not in ("read", "control"):
        logger.warning(f"Unknown mcityMode {resolved_mode!r}, falling back to read")
        resolved_mode = "read"
    if resolved_mode == "control" and not resolved_agent:
        logger.warning("mcityMode is control but mcityAgentId is empty; running in read mode")
        resolved_mode = "read"
    cfg["mode"] = resolved_mode

    cfg["http_timeout"] = _number(_config("mcityHttpTimeout", DEFAULT_HTTP_TIMEOUT),
                                  DEFAULT_HTTP_TIMEOUT, 1.0, 60.0)
    cfg["confirm_timeout"] = _number(_config("mcityConfirmTimeout", DEFAULT_CONFIRM_TIMEOUT),
                                     DEFAULT_CONFIRM_TIMEOUT, 0.0, 60.0)
    cfg["max_result_chars"] = int(_number(_config("mcityMaxResultChars", DEFAULT_MAX_RESULT_CHARS),
                                          DEFAULT_MAX_RESULT_CHARS, 200, 20000))
    cfg["action_min_gap"] = _number(_config("mcityActionMinInterval", DEFAULT_ACTION_MIN_GAP),
                                    DEFAULT_ACTION_MIN_GAP, 0.0, 60.0)

    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown"
    default_instance = f"omegaclaw:{resolved_agent}:{hostname}"
    instance = _text(_config("mcityClientInstanceId", default_instance)) or default_instance
    cfg["client_instance_id"] = re.sub(r"[^A-Za-z0-9._:@-]", "-", instance)[:128]

    merchants = _config("mcityTradeMerchants", [])
    if isinstance(merchants, str):
        merchants = merchants.split(",")
    if not isinstance(merchants, (list, tuple)):
        merchants = []
    cfg["trade_merchants"] = tuple(
        name for name in (_norm_arg(entry) for entry in merchants) if name
    )
    cfg["trade_max_quantity"] = int(_number(_config("mcityTradeMaxQuantity", 0), 0, 0, 1_000_000))

    # Roster store (docs/ARCHITECTURE-memory.md). The PG_* keys reach the
    # environment as OMEGACLAW_PG_* through config_get_by_key's OMEGACLAW_<key>
    # lookup, matching docker-compose.yml and Autotests/test_mcity_store.py.
    backend = _text(_config("mcityMemoryBackend", DEFAULT_MEMORY_BACKEND)).lower()
    if backend not in ("postgres", "memory"):
        if backend:
            logger.warning(f"Unknown mcityMemoryBackend {backend!r}, "
                           f"falling back to {DEFAULT_MEMORY_BACKEND}")
        backend = DEFAULT_MEMORY_BACKEND
    cfg["memory_backend"] = backend
    # Environment FIRST, then the config system, then the default.
    #
    # _config reads OmegaClaw's config (config.yaml and the command line); it does
    # NOT look at the environment. Every OMEGACLAW_PG_* variable that
    # entrypoint.sh forwards through `env -i` was therefore inert, and the store
    # silently resolved to host=127.0.0.1 port=5433 - a TCP port that is not even
    # published any more - and failed with "fe_sendauth: no password supplied"
    # while the agent ran on the in-memory fallback. The password below already
    # read the environment directly; these now do the same.
    cfg["pg_host"] = (_text(os.environ.get("OMEGACLAW_PG_HOST", ""))
                      or _text(_config("PG_HOST", DEFAULT_PG_HOST))
                      or DEFAULT_PG_HOST)
    cfg["pg_port"] = int(_number(os.environ.get("OMEGACLAW_PG_PORT")
                                 or _config("PG_PORT", DEFAULT_PG_PORT),
                                 DEFAULT_PG_PORT, 1, 65535))
    # Both spellings are honoured: OMEGACLAW_PG_DB is what docker-compose.yml
    # and docs/reference-grounded-memory.md use, OMEGACLAW_PG_DBNAME is what
    # Autotests/test_mcity_store.py uses. They default to the same database.
    cfg["pg_dbname"] = (_text(os.environ.get("OMEGACLAW_PG_DB", ""))
                        or _text(os.environ.get("OMEGACLAW_PG_DBNAME", ""))
                        or _text(_config("PG_DB", ""))
                        or _text(_config("PG_DBNAME", ""))
                        or DEFAULT_PG_DBNAME)
    cfg["pg_user"] = (_text(os.environ.get("OMEGACLAW_PG_USER", ""))
                      or _text(_config("PG_USER", DEFAULT_PG_USER))
                      or DEFAULT_PG_USER)
    # The password deliberately bypasses _config: config_get_by_key logs every
    # value it resolves, and no credential may ever reach a log line. It still
    # arrives through the environment, as OMEGACLAW_PG_PASSWORD, exactly like
    # the other OMEGACLAW_PG_* keys resolved above.
    cfg["pg_password"] = os.environ.get("OMEGACLAW_PG_PASSWORD", "")
    return cfg


def _probe_gateway():
    """Prove the gateway is up AND that the deny-by-default rule is in place.
    A /mcity/ prefix that relays everything would be a general purpose proxy
    holding the master token, reachable from `shell`."""
    deny = _http("GET", "/", timeout=8.0)
    if int(deny.get("status") or 0) != 403:
        logger.error("Midnight City gateway check failed: GET /mcity/ did not return 403 "
                     f"(status={deny.get('status')}); the deny-by-default rule is missing")
        return False
    read = _http("GET", "/api/skill/merchants", timeout=8.0)
    if not read["ok"]:
        code, _detail = _reason(read)
        logger.error(f"Midnight City gateway check failed: merchants read returned {code}")
        return False
    return True


def _check_skill_names():
    try:
        import helper
    except Exception:
        try:
            from src import helper  # noqa: F401
        except Exception:
            logger.warning("Could not import helper to verify LLM_COMMANDS")
            return "unknown"
    commands = getattr(helper, "LLM_COMMANDS", None)
    if not commands:
        logger.warning("helper.LLM_COMMANDS is unavailable; skipping the skill name check")
        return "unknown"
    missing = sorted(SKILL_NAMES - set(commands))
    if missing:
        logger.error(f"mcity: skills missing from helper.LLM_COMMANDS: {missing} - "
                     "multi command turns will silently drop them")
        return "degraded"
    return "ok"


def startup(gateway_url=None, agent_id=None, mode=None):
    """Called once from loadOmegaClawPlugin. Never raises: a broken plugin is
    logged, it must not stop the agent."""
    global _cfg, _gateway_state, _skills_state, _started, _reconnect_at
    try:
        _cfg = _resolve_config(gateway_url, agent_id, mode)
        _started = True

        if not _cfg["agent_id"]:
            # Nobody asked for Midnight City. Make no third party request at all
            # and register nothing: this plugin ships enabled in plugins.yaml,
            # so an untouched OmegaClaw must not egress to midnight.city on boot
            # nor carry a kilobyte of dead skills in every prompt.
            _gateway_state = "off"
            logger.info("Midnight City plugin is idle: mcityAgentId is not set, "
                        "no skill is registered and no request is made")
            return _out(_line("STARTUP", "OK", (
                ("mode", _cfg["mode"]), ("agent", "none"),
                ("gateway", "off"), ("lease", "off"), ("skills", "none"))))

        gateway_ok = _probe_gateway()
        _gateway_state = "ok" if gateway_ok else "failed"
        if not gateway_ok:
            _drop_lease("failed", "gateway")
            # Degrade to observation only: without a proven gateway we must not
            # try to take a lease, and action skills would only mislead the LLM.
            _cfg["mode"] = "read"

        _skills_state = _check_skill_names()

        lease_state = "off"
        if is_control_mode():
            if not _connect():
                # Do not let the very first heartbeat tick burn a reconnect
                # attempt one second after a retryable boot failure.
                with _lock:
                    _reconnect_at = time.monotonic() + RECONNECT_GAP_SECONDS
            _start_heartbeat()
            atexit.register(_release_quietly)
        with _lock:
            lease_state = _lease_state
            lease = dict(_lease) if _lease else None

        pairs = [
            ("mode", _c("mode", "read")),
            ("agent", _c("agent_id", "") or "none"),
            ("gateway", _gateway_state),
            ("lease", lease_state),
        ]
        if lease is not None:
            pairs.append(("ttl", f"{int(lease['lease_ttl_ms'] / 1000)}s"))
            pairs.append(("hb", f"{int(_hb_interval(lease))}s"))
        pairs.append(("trade", trade_flag()))
        pairs.append(("skills", registration_mode()))
        if _skills_state == "degraded":
            pairs.append(("recogniser", "degraded"))
        return _out(_line("STARTUP", "OK", pairs))
    except BaseException as e:  # noqa: BLE001 - loader must survive this
        logger.exception(f"Midnight City plugin startup failed: {e}")
        return _out(_line("STARTUP", "FAILED", (("reason", "internal"),)))


def shutdown():
    """Stop the heartbeat thread and release the lease. Used by the tests and
    by an operator driving the plugin from `metta`."""
    global _hb_thread
    try:
        _hb_stop.set()
        with _lock:
            thread = _hb_thread
            _hb_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        released = _release_quietly()
        return _out(_line("SHUTDOWN", "OK", (("released", "yes" if released else "no"),)))
    except BaseException as e:  # noqa: BLE001
        logger.exception(f"Midnight City shutdown failed: {e}")
        return _out(_line("SHUTDOWN", "FAILED", (("reason", "internal"),)))


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def _agent_id():
    lease = _lease_snapshot()
    if lease is not None:
        return lease["agent_id"]
    return _c("agent_id", "")


def _skill_read(verb, endpoint):
    """GET one /api/skill/agents/<own agent>/<endpoint>. The agent id is always
    ours: no skill can aim a read at an arbitrary agent."""
    agent = _safe_id(_agent_id())
    if agent is None:
        return None, _failed(verb, "not_ready", "no agent id is configured")
    resp = _http("GET", f"/api/skill/agents/{agent}/{endpoint}")
    if not resp["ok"]:
        return None, _fail_http(verb, resp)
    _harvest_vitals(resp["json"])
    return resp["json"], None


@_guard("STATUS")
def status():
    with _lock:
        state = _lease_state
        detail = _lease_detail
        lease = dict(_lease) if _lease else None
        actions = _action_count
        thread = _hb_thread
    pairs = [
        ("mode", _c("mode", "read")),
        ("agent", _c("agent_id", "") or "none"),
        ("lease", state),
    ]
    if lease is not None:
        remaining = max(0, int((lease["expires_at_ms"] - _now_ms()) / 1000))
        pairs.append(("expires_in", f"{remaining}s"))
        pairs.append(("hb", f"{int(_hb_interval(lease))}s"))
    if is_control_mode():
        pairs.append(("heartbeat", "alive" if thread is not None and thread.is_alive() else "dead"))
    pairs.append(("actions", actions))
    pairs.append(("trade", "enabled" if trade_enabled() else "disabled"))
    pairs.append(("gateway", _gateway_state))
    if _skills_state == "degraded":
        pairs.append(("skills", "degraded"))
    if _degraded():
        # Reported, never probed: status must not pay a store init to answer.
        pairs.append(("store", "degraded"))
    if detail:
        pairs.append(("detail", detail))
    if not _started:
        pairs.append(("startup", "incomplete"))
    return _line("STATUS", "OK", pairs)


def _generic_read(verb, endpoint):
    payload, error = _skill_read(verb, endpoint)
    if error is not None:
        return error
    if payload is None:
        return _failed(verb, "upstream_invalid")
    return _line(verb, "OK", (), _flatten(payload))


@_guard("CONTEXT")
def context():
    return _generic_read("CONTEXT", "context")


@_guard("INVENTORY")
def inventory():
    return _generic_read("INVENTORY", "inventory")


@_guard("NEEDS")
def needs():
    return _generic_read("NEEDS", "needs")


@_guard("AREAS")
def areas():
    # A route_known refusal used to live here. It fired 52 times in one window
    # and produced no movement at all, while blocking a legitimate read - the
    # agent follows the numbered procedure in the mission, and travel now has a
    # step there instead. Refusals steer well when they replace a wrong action;
    # this one had nothing to replace, because moving was never in the procedure.
    payload, error = _skill_read("AREAS", "areas")
    if error is not None:
        return error
    items = _find_list(payload, "areas")
    if items is None:
        return _line("AREAS", "OK", (), _flatten(payload))

    # The world tells us a distance per area but never which area we are
    # standing in, and `context` reports only spaceId plus raw x/y. Without a
    # derived marker the agent cannot tell whether it has arrived, so a rule
    # like "walk to nexifuse unless you are already there" never terminates and
    # it re-walks every turn instead of moving on to working or talking.
    # Mark the single closest area as here=yes.
    closest_index = None
    closest_distance = None
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        try:
            distance = float(_get(item, "distance"))
        except (TypeError, ValueError):
            continue
        if closest_distance is None or distance < closest_distance:
            closest_distance = distance
            closest_index = index

    rows = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        rows.append(_row((
            ("id", _plain(_get(item, "areaId", "id"))),
            ("name", _plain(_get(item, "name", "label"))),
            ("here", _plain("yes" if index == closest_index else "no")),
            ("movable", _plain(_get(item, "moveAreaAvailable"))),
            ("teleport", _plain(_get(item, "reachableByTeleport"))),
            ("dist", _plain(_get(item, "distance"))),
        )))
    return _line("AREAS", "OK", (("count", len(items)),), rows or ["- none"])


@_guard("AGENTS")
def agents():
    # Refuse a re-read we already know the answer to. reachable=0 told the agent
    # nobody could hear it and it called this 26 times in four minutes anyway -
    # stating the fact was not enough, exactly as with earned=enough. Repeat
    # suppression cannot catch it either: roster rows carry distances and
    # statuses that jitter, so no two bodies are byte-identical.
    #
    # Rate-limited rather than blocked, because people wake up: one read every
    # _ROSTER_RECHECK_MS always goes through, so the agent learns within half a
    # minute of the city changing.
    global _last_roster_read_ms
    if (_REACHABLE["n"] == 0
            and (_now_ms() - _REACHABLE["at_ms"]) <= _CAN_SPEAK_TTL_MS
            and (_now_ms() - _last_roster_read_ms) < _ROSTER_RECHECK_MS):
        left = int((_ROSTER_RECHECK_MS - (_now_ms() - _last_roster_read_ms)) / 1000) + 1
        # Route first. "Nobody can be reached" is true of HERE; it was firing 22
        # times a window while 55 free agents stood in central, and handing over
        # cmd=mcity-work each time - the one piece of advice that guarantees the
        # agent never finds them.
        return _promote_command(
            _failed("AGENTS", "nobody_reachable",
                    f"the roster said nobody here can receive a message and it "
                    f"was read moments ago; it is worth looking again in {left}s, "
                    "not now"),
            _travel_to_people_command() or _next_action_command())
    _last_roster_read_ms = _now_ms()
    payload, error = _skill_read("AGENTS", "agents")
    if error is not None:
        return error
    items = _find_list(payload, "agents")
    if items is None:
        return _line("AGENTS", "OK", (), _flatten(payload))

    now = _now_ms()
    roster = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = _parse_agent(item)
        _note_can_speak(entry)
        if isinstance(entry["name"], str):
            _remember_inbound(entry["name"])
        roster.append(entry)
    # Count here, in the skill the agent actually calls. The first version of
    # this counted inside _speak_candidates, which only runs on a failed speak,
    # so reachable= never appeared for a plain mcity-agents read - the exact call
    # it exists to make unnecessary.
    _REACHABLE["n"] = sum(1 for entry in roster
                          if _entry_reachable(entry) and _looks_speakable(entry["id"]))
    _REACHABLE["at_ms"] = _now_ms()

    # Ground every observation BEFORE rendering anything: the store keeps all
    # the fields, so nothing shown or dropped below is lost to the roster.
    upsert_ok = rank_ok = True
    ranked = []
    if roster and AgentObservation is not None:
        observed = [_agent_observation(entry, now)
                    for entry in roster if entry["id"]]
        if observed:
            _unused, upsert_ok = _store_call(
                lambda store: store.upsert_agents(observed))
        ranked, rank_ok = _store_call(
            lambda store: store.candidates(now_ms=now,
                                           cooldown_ms=SPEAK_COOLDOWN_MS,
                                           limit=len(roster)),
            default=[])

    # Ranked greeting candidates first (talking, then unspoken-oldest-nearest:
    # the anti-greeting-loop order), then everyone else. When the store failed
    # this turn the ranked list is empty and the whole roster degrades to
    # observation order - still talking-first, because that flag arrives in
    # the live payload rather than the store, then nearest-first.
    by_id = {entry["id"]: entry for entry in roster if entry["id"]}
    spoke = {row.agent_id: row.spoke_count for row in ranked}
    ranked_ids = [row.agent_id for row in ranked if row.agent_id in by_id]
    ranked_set = set(ranked_ids)
    rest = [entry for entry in roster if entry["id"] not in ranked_set]
    rest.sort(key=lambda entry: (
        _yn(entry["talking"]) != "yes",
        _number(entry["dist"], float("inf"), 0.0, float("inf"))))
    ordered = [by_id[agent_id] for agent_id in ranked_ids] + rest

    total = len(ordered) or 1
    rows = [(_agent_row(entry, spoke.get(entry["id"])), (total - index) / total)
            for index, entry in enumerate(ordered)]

    pairs = [("count", len(items))]
    if _degraded(upsert_ok, rank_ok):
        pairs.append(("store", "degraded"))
    head = _line("AGENTS", "OK", pairs)
    body = _project(head, "roster", "talking-then-unspoken-then-nearest", rows)
    return _line("AGENTS", "OK", pairs, body.splitlines() if body else ["- none"])


@_guard("NAVIGATION")
def navigation_options():
    payload, error = _skill_read("NAVIGATION", "navigation-options")
    if error is not None:
        return error
    if not isinstance(payload, dict):
        return _line("NAVIGATION", "OK", (), _flatten(payload))
    rows = []
    districts = payload.get("travelDistricts")
    rows.append("districts:")
    if isinstance(districts, list) and districts:
        for item in districts:
            rows.append(_row((("id", _plain(_get(item, "id", "districtId", "spaceId"))),
                              ("name", _plain(_get(item, "name", "label"))))))
    else:
        rows.append("- none")
    buildings = payload.get("enterableBuildings")
    rows.append("buildings:")
    if isinstance(buildings, list) and buildings:
        for item in buildings:
            rows.append(_row((("id", _plain(_get(item, "buildingId", "id"))),
                              ("name", _plain(_get(item, "name", "label"))))))
    else:
        rows.append("- none")
    exit_building = payload.get("exitBuilding")
    if isinstance(exit_building, dict):
        rows.append("exit: exit=" + str(_plain(_get(exit_building, "kind")) or "unknown"))
    else:
        rows.append("exit: exit=none")
    return _line("NAVIGATION", "OK", (), rows)


@_guard("MERCHANTS")
def merchants():
    resp = _http("GET", "/api/skill/merchants")
    if not resp["ok"]:
        return _fail_http("MERCHANTS", resp)
    payload = resp["json"]
    items = _find_list(payload, "merchants", "offers")
    if items is None:
        return _line("MERCHANTS", "OK", (), _flatten(payload))
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        position = item.get("position") if isinstance(item.get("position"), dict) else item
        space = _plain(_get(position, "spaceId"))
        x = _plain(_get(position, "x"))
        y = _plain(_get(position, "y"))
        pos = ",".join(part for part in (space, x, y) if part is not None) or None
        name = _get(item, "merchantName", "name")
        # A ready-to-use argument string. `item` is what the agent HANDS OVER,
        # which is the opposite of what it is usually reaching for, and naming
        # the wanted item instead is the single most common trade failure:
        #   (mcity-trade "to_go_food 50 Central Mart Outlet")
        #   -> not enough to_go_food to trade: have 0, need 50
        # Emitting the exact string to copy removes the inversion entirely, the
        # same way copying an agent id verbatim makes mcity-speak reliable.
        rows.append(_row((
            ("name", _merchant_label(name)),
            ("src", _plain(_get(item, "source"))),
            ("pos", pos),
            ("gives", _pair(_get(item, "paysQuantity"), _get(item, "paysItemId"))),
            ("for", _pair(_get(item, "acceptsQuantity"), _get(item, "acceptsItemId"))),
            ("item", _plain(_get(item, "itemId"))),
            ("min", _plain(_get(item, "minQuantity"))),
            ("batch", _plain(_get(item, "batchMultiple"))),
            ("cmd", _trade_cmd(item, name)),
        )))
    return _line("MERCHANTS", "OK", (("count", len(items)),), rows or ["- none"])


_TERMS_CACHE = {"at": 0.0, "by_name": {}}
_TERMS_TTL = 60.0


def _merchant_terms(name):
    """Trade terms for one merchant, briefly cached. None if unknown.

    Bounded like every other call here: one gateway read at most per TTL, and
    any failure returns None so trading proceeds unvalidated rather than
    breaking."""
    now = _now_ms() / 1000.0
    if now - _TERMS_CACHE["at"] > _TERMS_TTL or not _TERMS_CACHE["by_name"]:
        resp = _http("GET", "/api/skill/merchants")
        if not resp["ok"]:
            return None
        items = _find_list(resp["json"], "merchants", "offers")
        if items is None:
            return None
        table = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            label = _merchant_label(_get(item, "merchantName", "name"))
            if label is None:
                continue
            table[label] = {
                "pays": _get(item, "paysItemId"),      # what the merchant GIVES
                "takes": _get(item, "itemId"),         # what we HAND OVER
                "min": _get(item, "minQuantity"),
                "batch": _get(item, "batchMultiple"),
            }
        _TERMS_CACHE["at"] = now
        _TERMS_CACHE["by_name"] = table
    return _TERMS_CACHE["by_name"].get(name)


def _trade_cmd(item, name):
    """The exact mcity-trade argument for this merchant: what to hand over, at
    the smallest legal quantity, then the merchant name copied verbatim.

    Returns None when any part is missing, so a partial row never renders a
    command that would fail."""
    pay = _get(item, "itemId")
    minimum = _get(item, "minQuantity")
    batch = _get(item, "batchMultiple")
    # _merchant_label, not _plain: names contain spaces so _plain quarantines
    # them as untrusted, and a wrapped name is exactly what the agent must not
    # echo back. See _merchant_label's own note on this failure.
    label = _merchant_label(name)
    if pay is None or label is None:
        return None
    try:
        quantity = int(minimum if minimum is not None else 0)
        step = int(batch) if batch else 0
    except (TypeError, ValueError):
        return None
    if quantity <= 0:
        return None
    if step > 0 and quantity % step:
        quantity += step - (quantity % step)     # round up to a legal batch
    return f"{_plain(pay)} {quantity} {label}"


@_guard("RECENT-EVENTS")
def recent_events():
    payload, error = _skill_read("RECENT-EVENTS", "recent-events")
    if error is not None:
        return error
    items = _find_list(payload, "recentEvents", "events")
    if items is None:
        items = []
    rows = []
    for event in _newest([item for item in items if isinstance(item, dict)],
                         MAX_EVENT_ROWS, "tick"):
        event_payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        text = event_payload.get("text")
        if isinstance(text, str):
            _remember_inbound(text, event_payload.get("agentId"))
        event_id = _text(event.get("eventId"))
        rows.append(_row((
            ("ev", event_id[:8] or None),
            ("tick", _plain(event.get("tick"))),
            ("kind", _plain(event_payload.get("kind"))),
            ("agent", _plain(event_payload.get("agentId"))),
            ("text", _clean(text) if isinstance(text, str) and text else None),
        )))
    return _line("RECENT-EVENTS", "OK", (("count", len(items)),), rows or ["- none"])


def _thread_mine(item, own_id, sender=None):
    """"yes" if we spoke last, "no" if somebody is waiting on us, else None.

    Extracted so the vitals refresh can reach the same answer as the rendered
    row: two implementations of this would drift, and this is the flag the whole
    procedure turns on."""
    if own_id and isinstance(sender, str) and sender.strip():
        return "yes" if sender.strip() == own_id else "no"
    if not own_id:
        return None
    # No sender field exists in this world's payload. Two facts do:
    # pendingRecipientAgentId names whoever owes a reply, and the two message
    # counts say who has spoken. Either is enough to decide whether somebody is
    # waiting on us - which is the whole point of the flag, and the reason a
    # real question sat unanswered.
    pending = _get(item, "pendingRecipientAgentId")
    if isinstance(pending, str) and pending.strip():
        return "no" if pending.strip() == own_id else "yes"
    recipient = _get(item, "recipientAgentId")
    ours = _number(_get(item, "recipientMessageCount"), -1, 0, 10 ** 9)
    theirs = _number(_get(item, "initiatorMessageCount"), -1, 0, 10 ** 9)
    if (isinstance(recipient, str) and recipient.strip() == own_id
            and ours == 0 and theirs > 0):
        return "no"                      # they opened it, we have never replied
    return None


def _refresh_waiting_if_stale():
    """Keep waiting= current without the agent spending a turn on it.

    Measured: about a third of all turns were mcity-threads returning a list
    that had not changed, because the procedure had to poll to find out whether
    anyone was waiting. That is the same trap vitals already solved for hunger
    and inventory - the fact rides along on every result instead, and the agent
    only opens the thread list when the count says there is a reason to.

    One GET at most every _WAITING_REFRESH_MS, which is far cheaper than a whole
    turn. Never raises."""
    global _waiting_refresh_at_ms, _waiting_refreshing
    with _lock:
        if _waiting_refreshing:
            return
        if (_waiting_refresh_at_ms
                and (_now_ms() - _waiting_refresh_at_ms) < _WAITING_REFRESH_MS):
            return
        _waiting_refreshing = True
    try:
        payload, error = _own_threads("VITALS")
        if error is not None:
            return
        items = _find_list(payload, "threads") or []
        own_id = _c("agent_id", "")
        found = []
        for item in items:
            if not isinstance(item, dict):
                continue
            others = [str(part).strip()
                      for part in (_get(item, "participants", "participantIds",
                                        "with") or ())
                      if isinstance(part, str) and part.strip()
                      and part.strip() != own_id]
            if _thread_mine(item, own_id) != "no":
                continue
            if len(others) == 1 and ID_RE.match(others[0]) \
                    and _can_be_reached(others[0]) is not False:
                found.append(others[0])
        _WAITING["at_ms"] = _now_ms()
        _WAITING["ids"] = found
        _waiting_refresh_at_ms = _now_ms()
    except Exception:      # noqa: BLE001 - grounding must never break a skill
        pass
    finally:
        with _lock:
            _waiting_refreshing = False


def _own_threads(verb):
    """GET the thread list of our own agent. nginx injects the master token on
    /api/threads/, so this listing is the only thing that pins a thread read to
    the configured agent; without it the model could aim mcity-thread at any
    thread id the operator's token happens to be allowed to read."""
    agent = _safe_id(_agent_id())
    if agent is None:
        return None, _failed(verb, "not_ready", "no agent id is configured")
    resp = _http("GET", f"/api/agents/{agent}/threads?limit=50")
    if not resp["ok"]:
        return None, _fail_http(verb, resp)
    return resp["json"], None


@_guard("THREADS")
def threads(_ignored=None):
    payload, error = _own_threads("THREADS")
    if error is not None:
        return error
    items = _find_list(payload, "threads")
    if items is None:
        return _line("THREADS", "OK", (), _flatten(payload))

    own_id = _c("agent_id", "")
    # Learn reachability BEFORE building any row: the asleep flag, the ranking
    # and the waiting list are all decided inside the loop below, so a refresh
    # afterwards would arrive one turn too late to matter.
    _refresh_can_speak_if_unknown([
        part.strip()
        for item in items if isinstance(item, dict)
        for part in (_get(item, "participants", "participantIds", "with") or ())
        if isinstance(part, str) and part.strip() and part.strip() != own_id
    ])
    rows = []
    links = []
    waiting_ids = []
    index = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        thread_id = _get(item, "threadId", "id")
        participants = _get(item, "participants", "participantIds", "with")
        others = []
        if isinstance(participants, (list, tuple)):
            others = [str(part).strip() for part in participants
                      if isinstance(part, str) and part.strip()
                      and part.strip() != own_id]
            participants = ", ".join(str(part) for part in participants[:MAX_LIST_ITEMS])
        # The world names the two sides explicitly rather than shipping a
        # participants list, so without this every row rendered as a bare
        # "- thread=<id>" with no counterpart, no preview and no mine= flag. A
        # person had been waiting on a reply for hours and the agent could not
        # see the thread had anything in it.
        if not others:
            initiator = _get(item, "initiatorAgentId")
            recipient = _get(item, "recipientAgentId")
            for side in (initiator, recipient):
                if isinstance(side, str) and side.strip() and side.strip() != own_id:
                    others = [side.strip()]
                    participants = side.strip()
                    break
        # latestMessagePreview is the name the world actually uses; the "last*"
        # spellings below never matched anything, so no preview was ever shown.
        preview = _get(item, "latestMessagePreview", "messageBody",
                       "lastMessageBody", "preview", "lastMessage",
                       "lastMessagePreview")
        sender = _get(item, "lastMessageSenderId", "lastSenderAgentId",
                      "lastSenderId", "senderAgentId")
        if isinstance(preview, dict):
            if sender is None:
                sender = _get(preview, "senderAgentId", "agentId", "fromAgentId")
            preview = preview.get("text")
        if isinstance(preview, str):
            _remember_inbound(preview, sender)
        # mine=no is a person waiting on our reply; the ranking exists so that
        # those threads are the ones that survive the budget, exactly like the
        # ACTION REQUIRED imperative inside mcity-thread.
        mine = _thread_mine(item, own_id, sender)
        asleep = False
        if mine == "no" and len(others) == 1 and ID_RE.match(others[0]):
            # False only when the world has actually said so; unknown counts as
            # reachable, because refusing on a guess is what silenced the agent
            # before. A counterpart who cannot hear us is still waiting, but must
            # not be what the turn is spent on.
            asleep = _can_be_reached(others[0]) is False
            if not asleep:
                waiting_ids.append(others[0])
        # An unreachable person ranks below a reachable one: the budget should
        # spend its rows on threads the agent can actually answer this turn.
        band = 1.0 if mine == "no" else (0.3 if mine == "yes" else 0.6)
        if asleep:
            band = 0.5
        rows.append((_row((
            ("thread", _plain(thread_id)),
            # An agent id is the one thing here that MUST be echoable: it is the
            # first word of mcity-speak. Ids match ID_RE so _plain renders them
            # bare; anything else stays quarantined. The message preview below
            # is third-party text and is deliberately left wrapped.
            ("with", (_plain(others[0]) if len(others) == 1 and ID_RE.match(others[0])
                      else (_clean(participants) if participants else None))),
            ("mine", mine),
            # Only rendered when true, so a normal row is unchanged. Without it
            # the agent sees a person waiting, tries, and is told by the world -
            # in text it is instructed not to trust - that they are asleep.
            ("asleep", "yes" if asleep else None),
            ("last", _clean(preview) if isinstance(preview, str) and preview else None),
        )), band - min(index, 400) * 0.0005))
        index += 1
        if (AgentObservation is not None and isinstance(thread_id, str)
                and thread_id.strip() and len(others) == 1
                and ID_RE.match(others[0])):
            # A threads listing is what reveals which thread belongs to which
            # agent (mcity_store.AgentObservation docstring): record the link.
            links.append(AgentObservation(agent_id=others[0],
                                          thread_id=thread_id.strip()))

    link_ok = True
    if links:
        _unused, link_ok = _store_call(lambda store: store.upsert_agents(links))

    reachable_waiting = [i for i in waiting_ids if _can_be_reached(i) is not False]
    # waiting= is the number the procedure turns on, so it must count only the
    # people who can actually hear a reply. Live: 56 agents were reachable and
    # the agent still answered nobody, because every row it was told to answer
    # was someone in do-not-disturb and the rule had no way to fall through.
    pairs = [("count", len(items)), ("waiting-reachable", len(reachable_waiting))]
    if _degraded(link_ok):
        pairs.append(("store", "degraded"))
    head = _line("THREADS", "OK", pairs)
    waiting_ids = reachable_waiting
    _WAITING["at_ms"] = _now_ms()
    _WAITING["ids"] = waiting_ids
    body = _project(head, "threads", "waiting-first", rows)
    return _line("THREADS", "OK", pairs, body.splitlines() if body else ["- none"])


@_guard("THREAD")
def thread(arg=None):
    thread_id = _safe_id(arg)
    if thread_id is None:
        return _failed("THREAD", "bad_args", "give one thread id from mcity-threads")
    listing, error = _own_threads("THREAD")
    if error is not None:
        return error
    own = set()
    for item in _find_list(listing, "threads") or ():
        if not isinstance(item, dict):
            continue
        value = _get(item, "threadId", "id")
        if isinstance(value, str) and value.strip():
            own.add(urllib.parse.quote(value.strip(), safe=""))
    if thread_id not in own:
        # Authorisation for this route lives entirely in the operator's master
        # token, so it must be pinned here: only threads our own agent takes
        # part in may be read into the model's context.
        return _failed("THREAD", "not_found",
                       "that is not one of your threads, use an id from mcity-threads")
    resp = _http("GET", f"/api/threads/{thread_id}/messages?limit=100")
    if not resp["ok"]:
        return _fail_http("THREAD", resp)
    items = _find_list(resp["json"], "messages")
    if items is None:
        return _line("THREAD", "OK", (), _flatten(resp["json"]))
    rows = []
    last_was_mine = False
    last_sender = None
    for message in _oldest_tail([item for item in items if isinstance(item, dict)],
                                MAX_EVENT_ROWS, "sequenceNo", "tick"):
        # Real field names, confirmed live from the observer API: messageBody,
        # senderAgentId, recipientAgentId. The generic guesses below are kept as
        # fallbacks in case other deployments differ.
        text = _get(message, "messageBody", "responseText", "requestText",
                    "text", "body", "content")
        sender = _get(message, "senderAgentId", "agentId", "fromAgentId",
                      "senderId")
        recipient = _get(message, "recipientAgentId", "toAgentId")
        if isinstance(text, str):
            _remember_inbound(text, sender)
        if text is None and sender is None:
            # The message exists (count says so) but uses field names we do not
            # know, and an empty row renders as a bare "-": the agent then sees
            # a thread with nothing in it and never replies. Fall back to the
            # generic renderer so the content still reaches it, and so the real
            # field names show up in the logs.
            rows.extend(_flatten(message, "msg"))
            continue
        # "mine" lets the agent tell at a glance whether the last word in the
        # thread was its own, which is the whole basis of "reply only if they
        # spoke last".
        own_id = _c("agent_id", "")
        last_was_mine = bool(own_id and sender == own_id)
        last_sender = _plain(sender) if not last_was_mine else None
        rows.append(_row((
            ("from", _plain(sender)),
            ("mine", _plain("yes" if own_id and sender == own_id else "no")),
            ("to", _plain(recipient)),
            ("text", _clean(text) if isinstance(text, str) and text else None),
        )))
    # A rule buried in a mission paragraph loses to whatever the model feels
    # like doing: observed live as 11 threads where the other agent spoke last,
    # 0 replies, while it chose mcity-needs and mcity-work instead. Putting the
    # imperative in the RESULT text puts it directly in front of the model on
    # the turn it matters, which no amount of procedure wording achieved.
    if rows and last_sender is not None and not last_was_mine:
        rows.insert(0, "- ACTION REQUIRED: %s spoke last and is waiting for "
                       "your reply. Your next line this turn must be "
                       "mcity-speak %s followed by your answer."
                       % (last_sender, last_sender))
    return _line("THREAD", "OK", (("count", len(items)),), rows or ["- none"])


# --------------------------------------------------------------------------
# action outcome matching (exact port of the reference helper)
# --------------------------------------------------------------------------

def _failure_kinds(action):
    kind = action.get("kind")
    if kind in ("move_to", "travel_to_district", "enter_building", "exit_building"):
        return [kind, kind.replace("_", "")]
    return [kind]


def _failure_outcome(event, action):
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != "action_failed":
        return None
    if payload.get("agentId") != action["agentId"]:
        return None
    if payload.get("actionKind") not in _failure_kinds(action):
        return None
    if action["kind"] == "speak" and payload.get("targetAgentId") != action.get("targetAgentId"):
        return None
    reason = payload.get("reason")
    detail = _clean(reason) if isinstance(reason, str) and reason.strip() else ""
    return detail or f"the world rejected the {action['kind']} action"


def _destination_matches(destination, position):
    if not isinstance(destination, dict) or not isinstance(position, dict):
        return True
    if (isinstance(destination.get("spaceId"), str)
            and isinstance(destination.get("x"), int) and not isinstance(destination.get("x"), bool)
            and isinstance(destination.get("y"), int) and not isinstance(destination.get("y"), bool)):
        return (position.get("spaceId") == destination["spaceId"]
                and position.get("x") == destination["x"]
                and position.get("y") == destination["y"])
    return True


def _success_fields(event, action):
    """Extra k=v pairs when the event confirms the action, None otherwise.
    An empty list is a match, so callers must test `is None`."""
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("agentId") != action["agentId"]:
        return None
    kind = action["kind"]
    event_kind = payload.get("kind")

    if kind == "speak":
        if (event_kind == "agent_spoke"
                and payload.get("targetAgentId") == action.get("targetAgentId")
                and payload.get("text") == action.get("text")):
            return [("outcome", "delivered"),
                    ("thread", _plain(payload.get("threadId"))),
                    ("msg", _plain(payload.get("messageId"))),
                    ("seq", _plain(payload.get("sequenceNo")))]
        return None
    if kind == "shout_message":
        if event_kind == "agent_shouted" and payload.get("text") == action.get("text"):
            return [("outcome", "confirmed")]
        return None
    if kind == "move_to":
        if event_kind == "agent_arrived" and _destination_matches(action.get("destination"),
                                                                  payload.get("at")):
            return [("outcome", "confirmed")]
        return None
    if kind == "travel_to_district":
        to = payload.get("to")
        if (event_kind == "agent_transferred" and isinstance(to, dict)
                and to.get("spaceId") == action.get("districtId")):
            return [("outcome", "confirmed")]
        return None
    if kind in ("enter_building", "exit_building"):
        if event_kind == "agent_transferred":
            return [("outcome", "confirmed")]
        return None
    if kind == "trade":
        if (event_kind == "merchant_trade_completed"
                and payload.get("merchantName") == action.get("merchantName")):
            return [("outcome", "confirmed"),
                    ("sold", _pair(payload.get("soldQuantity"), payload.get("soldItemId"))),
                    ("got", _pair(payload.get("receivedQuantity"), payload.get("receivedItemId")))]
        return None
    if kind == "eat":
        if event_kind == "agent_ate":
            hunger = None
            before = _plain(payload.get("hungerBefore"))
            after = _plain(payload.get("hungerAfter"))
            if before is not None or after is not None:
                hunger = f"{before}->{after}"
            return [("outcome", "confirmed"),
                    ("item", _plain(payload.get("itemId"))),
                    ("hunger", hunger)]
        return None
    if kind in ("perform_job", "engage"):
        # perform_job confirms ONLY on resource_gathered: a non-resource job
        # stays PENDING even when it succeeded. The skill description says so.
        if event_kind == "resource_gathered":
            return [("outcome", "confirmed"),
                    ("item", _plain(payload.get("itemId"))),
                    ("qty", _plain(payload.get("quantity"))),
                    ("total", _plain(payload.get("total")))]
        return None
    if kind == "sleep":
        if event_kind == "agent_woke":
            return [("outcome", "confirmed")]
        return None
    return None


def _harvest_fallback(event, action):
    """activity_completed for a resource harvest: the action ran but nothing
    was gathered. Lower priority than a real resource_gathered event."""
    if action.get("kind") != "engage":
        return None
    activity = action.get("activity")
    canonical = HARVEST.get(_normalize_activity(activity))
    if canonical is None:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("kind") != "activity_completed":
        return None
    # The reference omits this check (mcity-control.mjs resourceHarvestCompletion
    # Outcome); worksites are shared and /recent-events returns entries that
    # merely mention us, so without it another agent's activity_completed would
    # confirm our harvest and, for crypto, promise a payout nobody started.
    if payload.get("agentId") != action["agentId"]:
        return None
    if HARVEST.get(_normalize_activity(payload.get("activity"))) != canonical:
        return None
    fields = [("outcome", "confirmed"), ("gathered", "no")]
    if canonical == "trade crypto":
        fields.append(("settlement", "pending"))
        fields.append(("expect", "meme_coin"))
        fields.append(("next", "run mcity-inventory then mcity-merchants"))
        fields.append(("reason", "the crypto terminal completed, settlement confirms later"))
    else:
        fields.append(("reason", "harvest completed without a resource event"))
    return fields


def _progress(event, action):
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("agentId") != action["agentId"]:
        return None
    kind = payload.get("kind")
    if kind == "agent_arrived":
        return "arrived"
    if kind in ("agent_moved", "agent_transferred"):
        return "in_progress"
    if action["kind"] == "sleep" and kind == "agent_shouted" and payload.get("text") == "\U0001f634":
        return "started"
    return None


# --------------------------------------------------------------------------
# mutations
# --------------------------------------------------------------------------

def _fetch_recent_events(agent_id):
    resp = _http("GET", f"/api/skill/agents/{agent_id}/recent-events"
                        f"?limit={RECENT_EVENT_LIMIT}")
    if not resp["ok"]:
        return None, resp
    items = _find_list(resp["json"], "recentEvents", "events")
    if items is None:
        items = []
    return [item for item in items if isinstance(item, dict)], resp


def _pace():
    """Ergonomic spacing only; the enforced budget is the nginx mcity_write
    zone, which the model cannot reach around with `shell` or `metta`."""
    gap = float(_c("action_min_gap", DEFAULT_ACTION_MIN_GAP))
    with _lock:
        last = _last_mutation_at
    if last <= 0 or gap <= 0:
        return None
    waited = time.monotonic() - last
    if waited >= gap:
        return None
    return f"wait {max(1, int(gap - waited + 0.5))}s"


def _submit(partial, verb):
    global _last_mutation_at, _action_count
    # Any action at all clears the read-repeat counters. The refusal exists to
    # break a look-only loop; once the agent acts, the loop is broken and the
    # next read must be answered in full even if the world has not moved yet.
    _LAST_READ.clear()
    lease = _lease_snapshot()
    if lease is None:
        return _failed(verb, "no_lease")
    agent_id = lease["agent_id"]
    # agentId is spread last and always taken from the lease: no caller can
    # ever target another agent, whatever the skill argument said.
    action = {**partial, "agentId": agent_id}

    safe_agent = _safe_id(agent_id)
    if safe_agent is None:
        return _failed(verb, "not_ready", "the leased agent id is unusable")

    before_events, resp = _fetch_recent_events(safe_agent)
    if before_events is None:
        return _fail_http(verb, resp)
    # Events without a truthy eventId are ignored rather than treated as new
    # forever: that can only cause a PENDING, never a false confirmation.
    before = {event.get("eventId") for event in before_events if event.get("eventId")}

    # Re-read the lease immediately before the POST. A successful heartbeat
    # replaces the whole lease including the token, and the snapshot above is
    # a full recent-events round trip old by now, so posting with it would use
    # a token the observer has already superseded (401 -> a misleading
    # reason=auth on a perfectly healthy lease). The reference does the same:
    # it renews the lease and posts with the freshly returned token.
    fresh = _lease_snapshot()
    if fresh is None:
        return _failed(verb, "no_lease")
    if fresh["agent_id"] != agent_id:
        return _failed(verb, "not_ready", "the lease moved to another agent")
    posted = _http("POST", "/api/actions", body=action, bearer=fresh["token"])
    if int(posted.get("status") or 0) in (401, 403):
        # The heartbeat can still rotate the token inside the POST itself. A
        # 401 means the action was not applied, so retrying once with the
        # current token cannot double-apply it.
        retry = _lease_snapshot()
        if (retry is not None and retry["agent_id"] == agent_id
                and retry["token"] != fresh["token"]):
            logger.warning("Midnight City action refused with a rotated lease token; "
                           "retrying once with the current one")
            posted = _http("POST", "/api/actions", body=action, bearer=retry["token"])
    with _lock:
        _last_mutation_at = time.monotonic()
    if not posted["ok"]:
        return _fail_http(verb, posted)
    with _lock:
        _action_count += 1

    budget = float(_c("confirm_timeout", DEFAULT_CONFIRM_TIMEOUT))
    deadline = time.monotonic() + budget
    checked = 0
    progress = None
    fallback = None
    while time.monotonic() < deadline:
        time.sleep(CONFIRM_POLL_INTERVAL)
        events, poll = _fetch_recent_events(safe_agent)
        if events is None:
            continue
        checked = len(events)
        for event in events:
            event_id = event.get("eventId")
            if not event_id or event_id in before:
                continue
            failure = _failure_outcome(event, action)
            if failure is not None:
                return _failed(verb, "action_failed", failure)
            success = _success_fields(event, action)
            if success is not None:
                return _line(verb, "OK", success + [("event", _plain(
                    (event.get("payload") or {}).get("kind")))])
            if fallback is None:
                fallback = _harvest_fallback(event, action)
            step = _progress(event, action)
            if step is not None:
                progress = step
        if fallback is not None:
            return _line(verb, "OK", fallback)
    return _line(verb, "PENDING", (
        ("outcome", "pending"),
        ("checked", checked),
        ("progress", progress),
        ("reason", f"no matching completion or failure within {int(budget)}s"),
    ))


def _mutate(verb, build):
    """Common preamble for every mutating skill."""
    blocked = _ensure_lease()
    if blocked is not None:
        return _failed(verb, blocked)
    busy = _pace()
    if busy is not None:
        return _failed(verb, "busy", busy)
    action, error = build()
    if error is not None:
        return error
    return _submit(action, verb)


def _destination_action(verb, arg, key, usage):
    def build():
        value = _norm_arg(arg)
        if not value or not ID_RE.match(value):
            return None, _failed(verb, "bad_args", usage)
        return {"kind": "move_to", "destination": {key: value}}, None
    return _mutate(verb, build)


@_guard("MOVE-AREA")
def move_area(arg=None):
    return _destination_action("MOVE-AREA", arg, "areaId",
                               "give one area id from mcity-areas")


@_guard("MOVE-AGENT")
def move_agent(arg=None):
    return _destination_action("MOVE-AGENT", arg, "targetAgentId",
                               "give one agent id from mcity-agents")


@_guard("MOVE-TILE")
def move_tile(arg=None):
    def build():
        parts = _split(arg, 2)
        if parts is None or not INT_RE.match(parts[0]) or not INT_RE.match(parts[1]):
            return None, _failed("MOVE-TILE", "bad_args", "give two whole numbers x and y")
        payload, error = _skill_read("MOVE-TILE", "context")
        if error is not None:
            return None, error
        space_id = None
        if isinstance(payload, dict):
            agent = payload.get("agent")
            position = agent.get("position") if isinstance(agent, dict) else None
            if isinstance(position, dict):
                space_id = position.get("spaceId")
        if not isinstance(space_id, str) or not space_id.strip():
            return None, _failed("MOVE-TILE", "upstream_invalid",
                                 "the world did not report your current space")
        return {"kind": "move_to",
                "destination": {"spaceId": space_id,
                                "x": int(parts[0]),
                                "y": int(parts[1])}}, None
    return _mutate("MOVE-TILE", build)


@_guard("TRAVEL-DISTRICT")
def travel_district(arg=None):
    def build():
        value = _norm_arg(arg)
        if not value or not ID_RE.match(value):
            return None, _failed("TRAVEL-DISTRICT", "bad_args",
                                 "give one district id from mcity-navigation")
        return {"kind": "travel_to_district", "districtId": value}, None
    return _mutate("TRAVEL-DISTRICT", build)


@_guard("ENTER-BUILDING")
def enter_building(arg=None):
    def build():
        value = _norm_arg(arg)
        if not value or not ID_RE.match(value):
            return None, _failed("ENTER-BUILDING", "bad_args",
                                 "give one building id from mcity-navigation")
        return {"kind": "enter_building", "buildingId": value}, None
    return _mutate("ENTER-BUILDING", build)


@_guard("EXIT-BUILDING")
def exit_building():
    return _mutate("EXIT-BUILDING", lambda: ({"kind": "exit_building"}, None))


def _someone_is_waiting():
    """The agent ids that owe a reply, per the last threads render, or []."""
    if not _WAITING["ids"] or not _WAITING["at_ms"]:
        return []
    if (_now_ms() - _WAITING["at_ms"]) > _WAITING_STALE_MS:
        return []
    return list(_WAITING["ids"])


def _refuse_while_hungry(verb):
    """A long action while hungry with food in the bag is never the right move."""
    if not _needs_to_eat():
        return None
    return _promote_command(
        _failed(verb, "eat_first",
                f"vitals says hunger={_VITALS.get('hunger')} and you are carrying "
                "food. This action takes minutes and you cannot eat while it "
                "runs"),
        "cmd=mcity-eat")


def _refuse_while_someone_waits(verb):
    """Long actions are refused while a real person is waiting on a reply.

    The mission has said in prose for several passes that answering outranks
    working. The agent started work anyway with two people waiting, and a long
    action makes it unreachable for the whole duration - the world rejects
    speech from a mid-action agent - so the thread dies at about sixty seconds
    and a person is left on read. The refusal names who to answer, so the next
    turn has an obvious move."""
    waiting = _someone_is_waiting()
    if not waiting:
        return None
    who = waiting[0]
    more = f" ({len(waiting)} people are waiting)" if len(waiting) > 1 else ""
    return _failed(verb, "someone_waiting",
                   f"{who} is waiting on your reply{more}. This action would "
                   "take minutes and the world refuses speech while it runs, so "
                   f"the thread would die unanswered. Reply first with mcity-speak "
                   f"{who} <your sentence>, then come back to this")


def _needs_to_eat():
    """True when the agent is hungry and is carrying something edible.

    Hunger climbed from 9 to 36 across a session in which the agent ate exactly
    zero times, because eating is step three and it rarely got past step two.
    Nothing in the harness protected it: the same enforcement that holds work
    back for a waiting person should hold it back for this."""
    hunger = (_VITALS.get("hunger") or "").lower()
    if not hunger.startswith(("hungry", "starving")):
        return False
    return any(food in (_VITALS.get("items") or "") for food in FOOD_ITEMS)


def _rich_enough():
    """True when the mission's own step four says to stop earning.

    'If hunger is normal and you hold more than two hundred meme_coin, skip
    earning and go to step five.' The agent held 18383 and kept grinding work,
    which is the same shape as the reply-first rule: it was prose, so it was not
    followed. Earning is the means; the agent is here to be present with people.
    """
    hunger = (_VITALS.get("hunger") or "").lower()
    if not hunger.startswith("normal"):
        return False
    match = re.search(r"meme_coin=(\d+)", _VITALS.get("items") or "")
    return bool(match) and int(match.group(1)) > _ENOUGH_MEME_COIN


@_guard("WORK")
def work():
    blocked = _refuse_while_hungry("WORK") or _refuse_while_someone_waits("WORK")
    if blocked is not None:
        return blocked
    # Only stop earning when there is genuinely something better to do. The
    # first version refused on wealth alone and produced 68 refused turns in
    # three minutes with nothing accomplished, where the agent had been
    # completing 27 work actions - strictly worse. Blocking the only available
    # action is not a priority, it is a dead end.
    # A NUDGE, not a wall. Blocking every turn produced 36 refusals and zero
    # messages in three minutes, while windows where it fired 2-8 times
    # delivered 2-4. The agent reads the roster when told to and then returns to
    # work; refusing that constantly just removes the one thing it will do,
    # which is strictly worse than letting it earn between reminders.
    # Two regimes, because the evidence differs.
    #
    # Nobody reachable: a nudge once a minute. Blocking every turn here produced
    # 36 refusals and zero messages, while the agent kept earning between them.
    #
    # Somebody reachable: refuse every time. Measured this pass - the agent took
    # the route, reached central with reachable=2, then called mcity-work, and a
    # hacker's worksite is the crypto terminal back inside the hacker house. Work
    # walked it home and the world re-engaged it there, undoing the journey it
    # had just made. When there is a person to talk to, earning more money it
    # does not need is not worth crossing the city to give up.
    # A reminder, not a wall. The experiment this replaces removed work entirely
    # while the money was not needed, to test the last hypothesis standing:
    #
    #   69 prompts named a reachable person in talk-to=  ->  0 speaks
    #   55 work attempts refused, every one                ->  0 speaks
    #
    # Taking the preferred action away did not produce speech, it produced 55
    # refusals and an agent doing nothing at all. This model answers people
    # readily - every message it has delivered was a reply to somebody named in
    # waiting= - and does not open a conversation, whatever the harness offers.
    # That is a fact about the model, and blocking its one useful activity to
    # protest it only makes the agent useless as well as quiet.
    global _last_rich_nudge_ms
    due = (_now_ms() - _last_rich_nudge_ms) >= _RICH_NUDGE_EVERY_MS
    alternative = _reachable_opener() if (due and _rich_enough()) else None
    if alternative and not _someone_is_waiting():
        _last_rich_nudge_ms = _now_ms()
        return _promote_command(
            _failed("WORK", "rich_enough",
                    "you hold well over the two hundred meme_coin the mission "
                    "calls enough and you are not hungry, so earning more is not "
                    "what this turn is for - go and be with people"),
            alternative)
    # Back off inside a contention burst. Measured over twelve minutes: failures
    # arrive in runs of one or two, occasionally six to nine, so an immediate
    # retry during a run is a request the world has just answered. The agent is
    # already at earned=enough, so a delayed job costs it nothing, and this world
    # is shared with other people's agents - not spending the call is the polite
    # default. The window is short enough that a freed terminal is picked up
    # within a few seconds.
    global _worksite_busy_until_ms
    if _now_ms() < _worksite_busy_until_ms:
        left = int((_worksite_busy_until_ms - _now_ms()) / 1000) + 1
        # Carry a command. The backoff fired 38 times in eight minutes and each
        # one cost a whole turn to discover; every other refusal in this plugin
        # hands over the next move instead of only saying no.
        return _promote_command(
            _failed("WORK", "worksite_busy",
                    f"every worksite here was taken moments ago, so this is "
                    f"paused for another {left}s rather than spending a call the "
                    "world has just answered"),
            _reachable_opener() or _next_action_command())
    result = _mutate("WORK", lambda: ({"kind": "perform_job"}, None))
    if "no available" in (result or "").lower() and "worksite" in (result or "").lower():
        _worksite_busy_until_ms = _now_ms() + _WORKSITE_BACKOFF_MS
    elif "MCITY-WORK-OK" in (result or "") or "MCITY-WORK-PENDING" in (result or ""):
        _worksite_busy_until_ms = 0
    # "no available hacker worksite" is contention, not a bad call: every
    # terminal in this room is taken by one of the 52 agents permanently engaged
    # here. It is also the same answer as "nobody here can talk" - the room is
    # the problem - and this failure is where the agent is actually looking, 22
    # times in one window against 10 successes.
    if "no available" in (result or "").lower() and "worksite" in (result or "").lower():
        go = _travel_to_people_command()
        if go:
            # Into the HEAD line, not appended below it. Appended, this exact
            # hint was shown 11 times and produced zero move attempts; the only
            # thing that has ever moved this agent is a cmd= in the first line.
            result = _promote_command(result, go)
    return result


@_guard("EAT")
def eat():
    """Eat from inventory, and on failure state what is actually held.

    The world's refusal arrives as prose inside MC_UNTRUSTED markers, which the
    agent is correctly instructed never to obey. Observed consequence: it read
    "not enough edible food to eat", discounted it as untrusted world text,
    trusted its own stale history saying it had bought fish, and re-issued eat
    eighteen times in ten minutes while starving.

    So we restate the fact in TRUSTED harness text, derived from our own
    inventory read rather than from anything the world said. `holding=` is
    ground truth the agent may act on; the world's prose stays quarantined.

    The opposite failure then appeared: once fed, it issued eat 88 times in ten
    minutes against "agent is not hungry", and the holding= hint made it worse
    by confirming it still had food. Eating when not hungry cannot succeed, so
    it is refused here from our own vitals - no world call, and the reason is
    stated in trusted text rather than quarantined prose."""
    hunger = _VITALS.get("hunger") or ""
    fresh = _VITALS.get("at_ms") and (_now_ms() - _VITALS["at_ms"]) <= _VITALS_STALE_MS
    if fresh and hunger.startswith(("normal", "full", "fed")):
        return _out(_failed("EAT", "not_hungry",
                            f"vitals says hunger={hunger}; eating only works when "
                            "hungry or starving, so do something else this turn"))
    result = _mutate("EAT", lambda: ({"kind": "eat"}, None))
    if "MCITY-EAT-FAILED" not in (result or ""):
        return result
    payload, error = _skill_read("EAT", "inventory")
    if error is not None or not isinstance(payload, dict):
        return result
    held = payload.get("inventory")
    if not isinstance(held, dict):
        return result
    if held:
        summary = " ".join(f"{name}={_plain(count)}" for name, count in sorted(held.items()))
    else:
        summary = "nothing"
    return _out(f"{result}\nholding={summary}")


@_guard("SLEEP")
def sleep_action(arg=None):
    def build():
        value = _norm_arg(arg)
        if not value or not ID_RE.match(value):
            return None, _failed("SLEEP", "bad_args", "give one area id from mcity-areas")
        return {"kind": "sleep",
                "location": {"areaId": value},
                "durationMs": SLEEP_DURATION_MS}, None
    return _mutate("SLEEP", build)


@_guard("HARVEST")
def harvest(arg=None):
    blocked = _refuse_while_hungry("HARVEST") or _refuse_while_someone_waits("HARVEST")
    if blocked is not None:
        return blocked

    def build():
        parts = _split(arg, 2)
        if parts is None or not ID_RE.match(parts[0]):
            return None, _failed("HARVEST", "bad_args",
                                 "give one area id then the activity")
        canonical = HARVEST.get(_normalize_activity(parts[1]))
        if canonical is None:
            return None, _failed("HARVEST", "bad_args",
                                 "use chop wood or mine ore or trade crypto")
        return {"kind": "engage",
                "location": {"areaId": parts[0]},
                "activity": canonical,
                "durationMs": ENGAGE_DURATION_MS}, None
    return _mutate("HARVEST", build)


@_guard("SPEAK")
def speak(arg=None):
    global _dnd_streak
    sent = {}

    def build():
        parts = _split(arg, 2)
        if parts is None or not ID_RE.match(parts[0]):
            return None, _failed("SPEAK", "bad_args",
                                 "first word is the agent id, then your sentence")
        text = parts[1]
        if not text:
            return None, _failed("SPEAK", "bad_args", "the message is empty")
        if _is_echo(text):
            return None, _failed("SPEAK", "bad_args",
                                 "do not repeat text written by another agent")
        # The same greeting, to the same person, twice in a row - observed live,
        # word for word. Delivered twice it reads as a bot, which is the one
        # thing this agent is meant not to be.
        said_before = _SAID.get((parts[0], _norm_arg(text).lower()))
        if said_before and (_now_ms() - said_before) <= _SAID_TTL_MS:
            ago = int((_now_ms() - said_before) / 1000)
            return None, _failed("SPEAK", "already_said",
                                 f"you sent {parts[0]} these exact words {ago}s "
                                 "ago and it was delivered. Say something new, "
                                 "or answer what they actually replied - read it "
                                 "with cmd=mcity-threads")
        # Refuse locally while an action is running. The world rejects it anyway
        # with "speaker is in do not disturb mode" - 6 of 6 observed failures were
        # at status=busy - so the round trip buys nothing, and the refusal can say
        # the one thing the world's answer does not: that waiting is the way out.
        #
        # Refresh first. Gating on already-fresh vitals made this refusal nearly
        # dead: over 25 minutes the agent attempted 107 speaks while vitals were
        # harvested only 10 times, so the doomed writes sailed past the check.
        # _refresh_vitals_if_stale is a no-op when vitals are fresh, so the cost is
        # at worst one GET in place of a POST that was going to fail anyway.
        #
        # This lives INSIDE build, not ahead of _mutate: build runs after the lease
        # check (which is what enforces read mode) and after the arguments parse,
        # so a read-mode session and a malformed call still fail for their own
        # reason instead of spending a request on vitals.
        # While busy, RATE-LIMIT the attempt instead of refusing it. The old hard
        # refusal was built on 6 of 6 speak failures correlating with status=busy
        # and on the phrase "speaker is in do not disturb mode". Two things since
        # then undermine it: the roster shows 283 nearby agents of which nearly
        # all are busy or traveling, and those agents are plainly conversing -
        # one of them opened a thread with us - so if busy blocked speech nobody
        # in this city could talk. The phrase may well describe the RECIPIENT,
        # which is what the mission text means by it elsewhere.
        #
        # The cost calculus also inverted. A doomed round trip cost three minutes
        # when the refusal was written; a decision now takes about five seconds.
        # Being wrong about the world costs a real person their reply, so let the
        # world decide and keep only a flood guard.
        # The busy rate-limit that stood here is gone: the world answered, and
        # what it rejects is "target is sleeping". The speaker's own status was
        # never the blocker, so gating on it silenced the agent for nothing.
        # What IS worth remembering is who was asleep, so the agent does not
        # spend turn after turn on someone who cannot hear it.
        # Symmetry: an agent inside a live engagement cannot be reached, and
        # that includes us. Two safety valves, because the last time a rule like
        # this existed it silenced the agent for hours: it never applies when
        # somebody reachable is waiting on a reply, and it always lets an attempt
        # through every _SELF_PROBE_MS so the world can prove us wrong.
        global _last_self_probe_ms
        if (_VITALS.get("engaged") and _VITALS.get("at_ms")
                and (_now_ms() - _VITALS["at_ms"]) <= _VITALS_STALE_MS
                and not _someone_is_waiting()
                and (_now_ms() - _last_self_probe_ms) < _SELF_PROBE_MS):
            wait = _VITALS.get("busy_for")
            when = f" for another {wait}s" if wait else ""
            return None, _failed("SPEAK", "self_engaged",
                                 f"you are mid-action{when} and the world refuses "
                                 "speech from a mid-action agent, measured 50 "
                                 "times out of 50. Let it finish, or leave with "
                                 f"{_escape_command()}")
        if _VITALS.get("engaged"):
            _last_self_probe_ms = _now_ms()
        if _can_be_reached(parts[0]) is False:
            others = [i for i in _someone_is_waiting()
                      if i != parts[0] and _can_be_reached(i) is not False]
            # The row already said asleep=yes and the agent spoke to them anyway:
            # 35 attempts in one window, every one refused here, every target
            # already flagged. It does not act on flags or on prose telling it to
            # do something else - it acts on a whole command. So give it one.
            # A complete command again, not "cmd=mcity-speak <id> <your
            # sentence>": the placeholder form was measured at 63 emissions for
            # one speak. mcity-threads stands alone, lands on the waiting row,
            # and carries what that person actually said - which is what the
            # reply needs anyway. Live: 18 sends to unreachable people while a
            # reachable person was waiting the whole time.
            alt = (f" {others[0]} is waiting and CAN hear you - read them and "
                   f"reply: cmd=mcity-threads" if others
                   else f" Nobody waiting can hear you. {_next_action_command()}")
            return None, _failed("SPEAK", "unreachable",
                                 f"the world reports {parts[0]} cannot receive a "
                                 f"message right now (asleep or away).{alt}")
        sent["agent_id"], sent["text"] = parts[0], text
        # The text is NOT sanitised beyond the whitespace collapse of
        # _norm_arg: confirmation needs payload.text == action.text byte for byte.
        return {"kind": "speak", "targetAgentId": parts[0], "text": text}, None

    result = _mutate("SPEAK", build)
    if sent and result.startswith("MCITY-SPEAK-OK") \
            and "outcome=delivered" in result.partition("\n")[0]:
        # Ground the delivery: spoke_count is the anti-greeting-loop counter
        # that candidates() ranks on. A store failure must never fail a speak
        # that already happened; it only marks the turn as ungrounded.
        _SAID[(sent["agent_id"], _norm_arg(sent["text"]).lower())] = _now_ms()
        _prune(_SAID, _SAID_TTL_MS, lambda v: v)
        _unused, spoken_ok = _store_call(
            lambda store: store.mark_spoken(sent["agent_id"], _now_ms(),
                                            sent["text"]))
        if _degraded(spoken_ok):
            result = _stamp_degraded(result)
        _dnd_streak = 0
        return result
    if "MCITY-SPEAK-FAILED" in (result or ""):
        # Remember a sleeping target. The world states this in prose inside the
        # untrusted markers, which the agent is correctly told never to obey - so
        # the fact has to be captured here, where it can be acted on, rather than
        # left for the model to notice and trust.
        lowered = (result or "").lower()
        # THREE distinct world refusals, and the difference decides everything:
        #   "target is sleeping"                -> that person, unreachable
        #   "target is in do not disturb mode"  -> that person, unreachable
        #   "speaker is in do not disturb mode" -> us, and no other target helps
        # Matching "do not disturb" alone counted the target-side one against
        # ourselves and told the agent to walk away when the real answer was to
        # answer somebody else.
        # A fourth refusal: "target only talks to friends". Social, not
        # temporal, so it will not clear on its own - all the more reason not to
        # spend another turn on that person.
        if sent.get("agent_id") and ("target is sleeping" in lowered
                                     or "target is in do not disturb" in lowered
                                     or "only talks to friends" in lowered):
            _ASLEEP[sent["agent_id"]] = _now_ms()
            _prune(_ASLEEP, _ASLEEP_TTL_MS, lambda v: v)
        elif "speaker is in do not disturb" in lowered:
            # Speaker-side: a different target cannot fix it, so the candidate
            # list below would be misleading on its own.
            _dnd_streak += 1
            if _dnd_streak >= _DND_STREAK_HINT:
                where = _VITALS.get("space") or "where you are"
                move = _escape_command()
                result = _out(
                    f"{result}\nnote={_dnd_streak} replies refused as speaker "
                    f"do-not-disturb while you were mid-action at {where}. This "
                    "is about YOU, not the person you are answering, so trying "
                    "someone else will not help. The world keeps starting this "
                    f"activity for you; leaving is what ends it. {move}")
                return result
        elif "MCITY-SPEAK-OK" in (result or ""):
            _dnd_streak = 0
        suggestion = _speak_candidates()
        if suggestion:
            result = _out(f"{result}\n{suggestion}")
    return result


def _speak_candidates(limit=3):
    """Trusted, copy-ready ids of people who can actually be spoken to.

    A speak failure was a dead end: the agent held one id from an old thread,
    the world answered "target is sleeping", and it retried the same sleeping
    target six times because nothing told it who else was there. It never calls
    mcity-agents on its own, so the roster stayed empty and the ranking built to
    answer exactly this question was never asked.

    Failing is now the moment we look: one read, the observations land in the
    store, and the top candidates come back ranked talking-first then
    unspoken-oldest-nearest. Ids are rendered plainly so they can be copied
    verbatim, the way cmd= works for merchants."""
    payload, error = _skill_read("SPEAK", "agents")
    if error is not None:
        return None
    items = _find_list(payload, "agents")
    if not items:
        return None
    now = _now_ms()
    roster = [_parse_agent(item) for item in items if isinstance(item, dict)]
    for entry in roster:
        _note_can_speak(entry)
    _REACHABLE["n"] = sum(1 for entry in roster
                          if _entry_reachable(entry) and _looks_speakable(entry["id"]))
    _REACHABLE["at_ms"] = _now_ms()
    reachable = {entry["id"]: entry for entry in roster if entry["id"]}
    if AgentObservation is not None:
        observed = [_agent_observation(entry, now) for entry in roster if entry["id"]]
        if observed:
            _store_call(lambda store: store.upsert_agents(observed))
        ranked, _ok = _store_call(
            lambda store: store.candidates(now_ms=now,
                                           cooldown_ms=SPEAK_COOLDOWN_MS,
                                           limit=limit),
            default=[])
        ids = [row.agent_id for row in (ranked or [])
               if row.agent_id in reachable
               and _entry_reachable(reachable[row.agent_id])]
    else:
        ids = []
    if not ids:
        # Store unavailable: fall back to the live flags, through the SAME
        # verdict the rendered column and the cache use. Filtering on raw
        # canSpeak here - as this did - suggested agents who were mid-engagement
        # and would be refused by the very next check; filtering on isOpenToTalk,
        # as it did before that, was worse still, true for 283 of 285 agents
        # including all 165 who were asleep. Third copy of one rule, now gone.
        usable = [e for e in roster if e["id"] and _entry_reachable(e)]
        usable.sort(key=lambda e: _number(e["dist"], float("inf"), 0.0, float("inf")))
        ids = [e["id"] for e in usable[:limit]]
    if not ids:
        return None
    # The id is the part that must be copied; the name is decoration. Player
    # names are player-authored - a real injection surface, unlike merchant NPC
    # names - so they are shown only when _plain leaves them alone, never
    # unwrapped the way _merchant_label does.
    labels = []
    for agent_id in ids:
        label = _plain(agent_id)
        name = _plain(reachable.get(agent_id, {}).get("name"))
        if name and "MC_UNTRUSTED" not in str(name):
            label = f"{label} ({name})"
        labels.append(label)
    return "try-instead=" + " | ".join(labels)


@_guard("TRADE")
def trade(arg=None):
    fixups = []

    def build():
        if not trade_enabled():
            return None, _failed("TRADE", "disabled",
                                 "no merchant allowlist is configured")
        parts = _split(arg, 3)
        if parts is None or not INT_RE.match(parts[1] if len(parts) > 2 else ""):
            # The skill's argument placeholder is written with underscores
            # (item_id_quantity_and_merchant_name_in_quotes), so the model
            # imitates that shape and emits "crystal_50_Central Fresh Fish
            # Outlet". Accept it. The item id itself may contain underscores
            # ("meme_coin"), so anchor on the numeric quantity and let the
            # non-greedy item id expand only as far as it must.
            underscored = re.match(r"^(.+?)_(\d+)_(.+)$", _text(arg) or "")
            if underscored:
                parts = [underscored.group(1),
                         underscored.group(2),
                         underscored.group(3).strip()]
            elif parts is None:
                return None, _failed("TRADE", "bad_args",
                                     "give item id then quantity then merchant "
                                     "name, separated by spaces")
        item_id, quantity_text, merchant = parts[0], parts[1], parts[2]
        if not ID_RE.match(item_id):
            return None, _failed("TRADE", "bad_args", "the item id is not valid")
        if not INT_RE.match(quantity_text):
            return None, _failed("TRADE", "bad_args", "the quantity must be a whole number")
        quantity = int(quantity_text)
        maximum = int(_c("trade_max_quantity", 0))
        if quantity <= 0 or quantity > maximum:
            return None, _failed("TRADE", "bad_args",
                                 f"the quantity must be between 1 and {maximum}")
        # A single "*" entry means the operator has deliberately allowed any
        # merchant. The allowlist still has to be non-empty to enable trading
        # at all, so this stays an explicit opt-in rather than a default.
        allowed = _c("trade_merchants", ())
        if "*" not in allowed and merchant not in allowed:
            return None, _failed("TRADE", "disabled",
                                 "that merchant is not on the allowed list")
        # Accept the inverted argument. The model reaches for the thing it
        # WANTS and names that, so it emits the item the merchant PAYS rather
        # than the one it takes:
        #   (mcity-trade "to_go_food 50 Central Mart Outlet")
        #   -> not enough to_go_food to trade: have 0, need 50
        # It repeated that fifteen times across two sessions, through two
        # separate improvements to the merchant listing. This file already
        # forgives the underscore-shaped argument for the same reason; an
        # unambiguous inversion is no different. Corrected here, and the
        # correction is reported so the agent can learn the right form.
        terms = _merchant_terms(merchant)
        corrected = None
        if (terms and terms.get("takes") and terms.get("pays")
                and item_id == terms["pays"] and item_id != terms["takes"]):
            corrected = item_id
            item_id = terms["takes"]
            try:
                least = int(terms.get("min") or 0)
                step = int(terms.get("batch") or 0)
            except (TypeError, ValueError):
                least, step = 0, 0
            if quantity < least:
                quantity = least
            if step > 0 and quantity % step:
                quantity += step - (quantity % step)
            if quantity > maximum:
                return None, _failed("TRADE", "bad_args",
                                     f"the corrected quantity exceeds {maximum}")
        if corrected is not None:
            # Never travels in the action body - the world would reject an
            # unknown field. Reported to the agent after the call instead.
            fixups.append((corrected, item_id, quantity))
        return {"kind": "trade",
                "merchantName": merchant,
                "itemId": item_id,
                "quantity": quantity}, None
    result = _mutate("TRADE", build)
    if fixups:
        was, now, count = fixups[-1]
        result = _out(f"{result}\ncorrected=you named {_plain(was)}, which is what "
                      f"this merchant GIVES; handed over {count} {_plain(now)} instead")
    if "MCITY-TRADE-FAILED" not in (result or ""):
        return result
    # The world explains the inversion perfectly - "not enough to_go_food to
    # trade: have 0, need 50" - but says it inside MC_UNTRUSTED markers the
    # agent is correctly told never to obey, so it discounts the explanation
    # and retries the same wrong argument. Restate it in TRUSTED harness text
    # built from our own inventory read.
    payload, error = _skill_read("TRADE", "inventory")
    if error is not None or not isinstance(payload, dict):
        return result
    held = payload.get("inventory")
    if not isinstance(held, dict):
        return result
    summary = " ".join(f"{k}={_plain(v)}" for k, v in sorted(held.items())) or "nothing"
    return _out(f"{result}\nholding={summary}"
                "\nnote=the first value is what you HAND OVER and must be an item "
                "you already hold; copy the cmd= field from mcity-merchants")
