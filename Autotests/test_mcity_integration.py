"""Integration tests for the grounded-memory layer inside mcity_client.

docs/ARCHITECTURE-memory.md is the binding contract. These tests exercise the
seam the store and projection tests cannot: the live skill functions of
plugins/mcity/mcity_client.py calling the roster store and the Budgeter.
Fully hermetic - no live agent, no live database, no network: the transport
is a stubbed `_http` and the store is the in-memory backend selected through
the client's own configuration.

What must never regress:

  * a 284-agent payload renders INSIDE `max_result_chars` (never through the
    `_cap()` ...TRUNCATED path - positional truncation is the original
    defect) with an accurate rule-4 dropped-count footer,
  * roster rows carry `status` and the open/talking indicators, because the
    system prompt says "speak to one whose status is idle" and previously
    status never reached the model at all,
  * every observation field reaches the store (the old renderer kept only
    id/name/dist),
  * a confirmed mcity-speak records a spoke_count - the anti-greeting-loop
    counter candidates() ranks on - and a store failure never fails the
    speak that already happened,
  * threads ranking puts mine=no first: those are people waiting on a reply,
  * a store that raises on every call still yields a working skill result,
    marked store=degraded; an unreachable configured backend falls back to
    the in-memory store, also marked, never silently.

Run:
    OMEGACLAW_SKIP_LIVE_CLEANUP=1 python3 -m pytest Autotests/test_mcity_integration.py -q
"""
import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_PLUGIN = os.path.join(_REPO, "plugins", "mcity")
if _PLUGIN not in sys.path:
    sys.path.insert(0, _PLUGIN)

import mcity_client as mc                        # noqa: E402
from mcity_store.memory import InMemoryStore     # noqa: E402

OWN_ID = "user-agent-me"
AGENT_COUNT = 284
TALKER = "user-agent-200"

ROSTER_FOOTER_RE = re.compile(
    r"^\[roster: (?P<shown>\d+) of (?P<total>\d+) shown, "
    r"ranked by talking-then-unspoken-then-nearest\]$")
THREADS_FOOTER_RE = re.compile(
    r"^\[threads: (?P<shown>\d+) of (?P<total>\d+) shown, "
    r"ranked by waiting-first\]$")


# --------------------------------------------------------------------------
# harness: module reset, stubbed transport, payload builders
# --------------------------------------------------------------------------

def _reset():
    mc._cfg = {}
    mc._lease = None
    mc._lease_state = "off"
    mc._lease_detail = ""
    mc._gateway_state = "unknown"
    mc._skills_state = "unknown"
    mc._action_count = 0
    mc._last_mutation_at = 0.0
    mc._reconnects = 0
    mc._started = False
    mc._inbound.clear()
    mc._store = None
    mc._store_degraded = False
    mc._store_ready = False
    mc._store_retry_at = 0.0


@pytest.fixture(autouse=True)
def client(monkeypatch):
    """A started read-mode client wired for the in-memory backend. No test
    below may open a socket or touch a real database."""
    _reset()
    mc._cfg = {
        "gateway_url": "http://stub.invalid",
        "agent_id": OWN_ID,
        "mode": "read",
        "http_timeout": 2.0,
        "confirm_timeout": 0.3,
        "max_result_chars": 2000,
        "action_min_gap": 0.0,
        "client_instance_id": "test",
        "trade_merchants": (),
        "trade_max_quantity": 0,
        "memory_backend": "memory",     # hermetic: never a live database
        "pg_host": "127.0.0.1",
        "pg_port": 1,                   # a dead port, in case anything strays
        "pg_dbname": "omegaclaw",
        "pg_user": "omegaclaw",
        "pg_password": "",
    }
    mc._started = True
    mc._gateway_state = "ok"
    monkeypatch.setattr(mc, "CONFIRM_POLL_INTERVAL", 0.01)
    yield
    _reset()


def _ok(payload):
    return {"ok": True, "status": 200, "json": payload, "text": "", "err": None}


def _install_http(monkeypatch, routes):
    """Route (METHOD, path-without-query) -> payload dict or callable(body)."""
    def fake(method, path, body=None, bearer=None, timeout=None):
        handler = routes.get((method, path.split("?", 1)[0]))
        if handler is None:
            return {"ok": False, "status": 404, "json": None, "text": "",
                    "err": None}
        if callable(handler):
            return handler(body)
        return _ok(handler)
    monkeypatch.setattr(mc, "_http", fake)


def _install_store(store, degraded=False):
    """Bypass lazy init and plant a specific store, as if already built."""
    mc._store = store
    mc._store_ready = True
    mc._store_degraded = degraded
    mc._store_retry_at = 0.0


def _take_lease():
    """Hold a live lease without a heartbeat thread: mutations only."""
    mc._cfg["mode"] = "control"
    mc._lease = {"session_id": "session-test", "agent_id": OWN_ID,
                 "token": "midnight_TESTLEASE0001",
                 "expires_at_ms": mc._now_ms() + 300_000,
                 "heartbeat_interval_ms": 30_000, "lease_ttl_ms": 300_000}
    mc._lease_state = "active"


def _roster_payload():
    """284 agents shaped like the live observer answer, every field present.
    Agent 200 is actively addressing us but does not advertise openness:
    isTalkingToYou must outrank every nearer agent AND a closed door."""
    agents = []
    for index in range(AGENT_COUNT):
        agents.append({
            "agentId": f"user-agent-{index:03d}",
            "name": f"Citizen {index:03d}",
            "status": "idle" if index % 3 else "busy",
            "isOpenToTalk": bool(index % 3),
            "isTalkingToYou": False,
            "canSpeak": True,
            "isOnSameMap": True,
            "distance": 100 + index,
            "profession": "hacker" if index % 2 else "miner",
        })
    agents[200]["isTalkingToYou"] = True
    agents[200]["isOpenToTalk"] = False
    return {"agents": agents}


def _threads_payload(count):
    """Even-indexed threads have the OTHER agent speaking last (mine=no,
    a person waiting); odd-indexed ones end with our own message."""
    threads = []
    for index in range(count):
        other = f"user-agent-{index:03d}"
        sender = OWN_ID if index % 2 else other
        threads.append({
            "threadId": f"t{index}",
            "participants": [OWN_ID, other],
            "lastMessage": {"senderAgentId": sender,
                            "text": f"message number {index} with some padding"},
        })
    return {"threads": threads}


def _agents_routes():
    return {("GET", f"/api/skill/agents/{OWN_ID}/agents"): _roster_payload()}


def _threads_routes(count):
    return {("GET", f"/api/agents/{OWN_ID}/threads"): _threads_payload(count)}


def _speak_routes():
    """recent-events is empty before the action and carries the matching
    agent_spoke confirmation after it, like the reference fake observer."""
    events = []

    def recent(_body=None):
        return _ok({"recentEvents": list(events)})

    def actions(body):
        events.append({"eventId": f"e-{len(events)}", "tick": 1,
                       "payload": {"kind": "agent_spoke", "agentId": OWN_ID,
                                   "targetAgentId": body["targetAgentId"],
                                   "text": body["text"], "threadId": "t1",
                                   "messageId": "m1", "sequenceNo": 1}})
        return _ok({"accepted": True})

    return {("GET", f"/api/skill/agents/{OWN_ID}/recent-events"): recent,
            ("POST", "/api/actions"): actions}


class _BoomStore:
    """Every RosterStore method raises: a database that died mid-run."""

    def upsert_agents(self, observed):
        raise RuntimeError("boom")

    def mark_spoken(self, agent_id, at_ms, text):
        raise RuntimeError("boom")

    def candidates(self, *, now_ms, cooldown_ms, limit):
        raise RuntimeError("boom")

    def get(self, agent_id):
        raise RuntimeError("boom")

    def health(self):
        raise RuntimeError("boom")


def _check(result):
    """The agent-loop invariants plus the projection bound: a projected result
    fits max_result_chars exactly, WITHOUT the _cap() ...TRUNCATED path."""
    assert isinstance(result, str) and result.strip()
    assert result.startswith("MCITY-")
    assert '"' not in result and "'" not in result
    assert "\r" not in result
    assert len(result) <= mc._c("max_result_chars", mc.DEFAULT_MAX_RESULT_CHARS)
    assert not result.endswith("...TRUNCATED")
    return result


def _body_rows(result):
    return [line for line in result.splitlines() if line.startswith("- ")]


def _footer(result, pattern):
    matches = [pattern.match(line) for line in result.splitlines()]
    found = [match for match in matches if match]
    assert len(found) == 1, f"expected exactly one footer line in: {result}"
    return int(found[0].group("shown")), int(found[0].group("total"))


# --------------------------------------------------------------------------
# the 284-agent regression: budget, footer, ranking
# --------------------------------------------------------------------------

def test_284_agent_roster_fits_the_budget_with_an_accurate_footer(monkeypatch):
    _install_http(monkeypatch, _agents_routes())
    result = _check(mc.agents())
    assert result.startswith("MCITY-AGENTS-OK count=284")
    assert "store=degraded" not in result, \
        "the configured memory backend is an operator choice, not a fallback"

    rows = _body_rows(result)
    shown, total = _footer(result, ROSTER_FOOTER_RE)
    assert total == AGENT_COUNT
    assert shown == len(rows), "the footer must count exactly what is shown"
    assert 8 <= shown < AGENT_COUNT, \
        "the budget must carry a useful slice and the drop must be reported"


def test_talking_agent_outranks_every_nearer_agent(monkeypatch):
    _install_http(monkeypatch, _agents_routes())
    rows = _body_rows(_check(mc.agents()))
    assert f"id={TALKER}" in rows[0], \
        "someone already addressing us is the hard first key"
    assert "talking=yes" in rows[0]


def test_roster_rows_carry_status_and_talk_indicators(monkeypatch):
    _install_http(monkeypatch, _agents_routes())
    rows = _body_rows(_check(mc.agents()))
    for row in rows:
        assert " status=" in row, f"status missing from: {row}"
        assert " open=" in row, f"open indicator missing from: {row}"
    assert any("status=idle" in row for row in rows)
    assert sum("talking=yes" in row for row in rows) == 1


def test_agents_grounds_every_observation_field(monkeypatch):
    _install_http(monkeypatch, _agents_routes())
    _check(mc.agents())
    assert isinstance(mc._store, InMemoryStore)
    row = mc._store.get("user-agent-005")
    assert row.name == "Citizen 005"
    assert row.status == "idle"
    assert row.profession == "hacker"
    assert row.is_open_to_talk is True
    assert row.is_talking_to_you is False
    assert row.can_speak is True
    assert row.is_on_same_map is True
    assert row.dist == 105.0
    talker = mc._store.get(TALKER)
    assert talker.is_talking_to_you is True
    assert talker.is_open_to_talk is False
    busy = mc._store.get("user-agent-006")
    assert busy.status == "busy"


def test_spoken_agents_stop_crowding_the_roster(monkeypatch):
    # The anti-greeting-loop: once spoken to (inside the cooldown), an agent
    # leaves the candidate ranking and a fresh face takes the slot.
    _install_http(monkeypatch, _agents_routes())
    first = _check(mc.agents())
    assert "id=user-agent-001 " in first
    mc._store.mark_spoken("user-agent-001", mc._now_ms(), "hello")
    second = _check(mc.agents())
    assert "id=user-agent-001 " not in second
    shown, total = _footer(second, ROSTER_FOOTER_RE)
    assert total == AGENT_COUNT and shown == len(_body_rows(second))


# --------------------------------------------------------------------------
# speak: grounding the delivery
# --------------------------------------------------------------------------

def test_confirmed_speak_records_a_spoke_count(monkeypatch):
    _install_http(monkeypatch, _speak_routes())
    _take_lease()
    result = _check(mc.speak("user-agent-007 hello there friend"))
    assert result.startswith("MCITY-SPEAK-OK")
    assert "outcome=delivered" in result
    assert "store=degraded" not in result
    row = mc._store.get("user-agent-007")
    assert row is not None
    assert row.spoke_count == 1
    assert row.last_spoken_text == "hello there friend"


def test_unconfirmed_speak_records_nothing(monkeypatch):
    routes = _speak_routes()
    routes[("POST", "/api/actions")] = lambda body: _ok({"accepted": True})
    _install_http(monkeypatch, routes)
    _take_lease()
    result = _check(mc.speak("user-agent-007 anyone home"))
    assert result.startswith("MCITY-SPEAK-PENDING")
    assert mc._store is None or mc._store.get("user-agent-007") is None


def test_a_store_that_cannot_record_never_fails_the_speak(monkeypatch):
    _install_http(monkeypatch, _speak_routes())
    _take_lease()
    _install_store(_BoomStore())
    result = _check(mc.speak("user-agent-007 hello again"))
    assert result.startswith("MCITY-SPEAK-OK")
    assert "outcome=delivered" in result
    assert "store=degraded" in result.splitlines()[0]


# --------------------------------------------------------------------------
# threads: people waiting come first
# --------------------------------------------------------------------------

def test_threads_prioritise_people_waiting(monkeypatch):
    _install_http(monkeypatch, _threads_routes(6))
    result = _check(mc.threads())
    assert result.startswith("MCITY-THREADS-OK count=6")
    rows = _body_rows(result)
    flags = ["mine=no" if "mine=no" in row else "mine=yes" for row in rows]
    assert flags == sorted(flags), \
        f"every mine=no row must precede every mine=yes row: {rows}"
    assert flags.count("mine=no") == 3 and flags.count("mine=yes") == 3
    assert "thread=t0" in rows[0], "recency order must hold inside a band"


def test_threads_footer_counts_dropped_threads(monkeypatch):
    _install_http(monkeypatch, _threads_routes(40))
    result = _check(mc.threads())
    rows = _body_rows(result)
    shown, total = _footer(result, THREADS_FOOTER_RE)
    assert total == 40
    assert shown == len(rows)
    assert 0 < shown < 40
    assert all("mine=no" in row for row in rows), \
        "under pressure the surviving rows must be the people waiting"


def test_threads_record_the_thread_to_agent_links(monkeypatch):
    _install_http(monkeypatch, _threads_routes(4))
    _check(mc.threads())
    assert mc._store.get("user-agent-002").thread_id == "t2"


# --------------------------------------------------------------------------
# degradation: a broken store must never break a skill
# --------------------------------------------------------------------------

def test_a_store_that_raises_on_every_call_still_serves(monkeypatch):
    _install_http(monkeypatch, _agents_routes())
    _install_store(_BoomStore())
    result = _check(mc.agents())
    assert result.startswith("MCITY-AGENTS-OK")
    assert "store=degraded" in result.splitlines()[0]
    rows = _body_rows(result)
    assert rows, "a degraded turn still renders the roster"
    assert f"id={TALKER}" in rows[0], \
        "talking-first survives degradation: the flag is in the live payload"
    shown, total = _footer(result, ROSTER_FOOTER_RE)
    assert total == AGENT_COUNT and shown == len(rows)


def test_a_raising_store_degrades_threads_too(monkeypatch):
    _install_http(monkeypatch, _threads_routes(6))
    _install_store(_BoomStore())
    result = _check(mc.threads())
    assert result.startswith("MCITY-THREADS-OK")
    assert "store=degraded" in result.splitlines()[0]
    assert _body_rows(result)


def test_unreachable_backend_falls_back_to_memory_and_reports(monkeypatch):
    # pg_port 1 is closed; without psycopg the same path fails at import.
    # Either way construction must not raise, the fallback must be the
    # in-memory store and the degradation must be visible in every result.
    mc._cfg["memory_backend"] = "postgres"
    _install_http(monkeypatch, _agents_routes())
    result = _check(mc.agents())
    assert result.startswith("MCITY-AGENTS-OK")
    assert "store=degraded" in result.splitlines()[0]
    assert isinstance(mc._store, InMemoryStore)
    assert mc._store_degraded is True
    assert "store=degraded" in _check(mc.status())


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def test_resolve_config_reads_the_omegaclaw_pg_environment(monkeypatch):
    for key in ("OMEGACLAW_PG_HOST", "OMEGACLAW_PG_PORT", "OMEGACLAW_PG_DB",
                "OMEGACLAW_PG_DBNAME", "OMEGACLAW_PG_USER",
                "OMEGACLAW_mcityMemoryBackend"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OMEGACLAW_PG_PASSWORD", "test-secret-0001")
    cfg = mc._resolve_config("http://stub.invalid", "agent-1", "read")
    assert cfg["memory_backend"] == "postgres"       # the shipped default
    assert cfg["pg_host"] == "127.0.0.1"
    assert cfg["pg_port"] == 5433                    # pgvector, never 5432
    assert cfg["pg_dbname"] == "omegaclaw"
    assert cfg["pg_user"] == "omegaclaw"
    assert cfg["pg_password"] == "test-secret-0001"  # straight from the env


# ---------------------------------------------------------------------------
# Regression: merchant offer terms must survive rendering
# ---------------------------------------------------------------------------

def test_get_reads_nested_offer_terms():
    """The merchants endpoint nests what a merchant gives/takes under `offer`
    and the payment terms under `trade`. Reading only `trade` blanked `gives`
    and `for` on every row, so the agent could not tell which merchant sold
    food nor that fish costs crystal - it starved holding 14,200 crystal."""
    merchant = {
        "name": "Central Fresh Fish Outlet",
        "offer": {"acceptsItemId": "crystal", "acceptsQuantity": 50,
                  "paysItemId": "fish", "paysQuantity": 1},
        "trade": {"itemId": "crystal", "minQuantity": 50, "batchMultiple": 50},
    }
    assert mc._get(merchant, "paysItemId") == "fish"
    assert mc._get(merchant, "paysQuantity") == 1
    assert mc._get(merchant, "acceptsItemId") == "crystal"
    assert mc._get(merchant, "acceptsQuantity") == 50
    # `trade` still wins for the payment terms it owns.
    assert mc._get(merchant, "itemId") == "crystal"
    assert mc._get(merchant, "minQuantity") == 50
    assert mc._get(merchant, "batchMultiple") == 50
    # Top level still takes precedence over both nested objects.
    assert mc._get({"itemId": "top", "trade": {"itemId": "nested"}},
                        "itemId") == "top"


def test_trade_cmd_is_copy_ready():
    """The agent must be able to lift cmd= verbatim into mcity-trade.

    Two ways this silently breaks: naming the RECEIVED item instead of the one
    handed over (the world then answers 'not enough to_go_food: have 0'), and
    emitting the merchant name through _plain, which quarantines any name
    containing spaces as untrusted and so cannot be echoed back."""
    merchant = {
        "name": "Central Fresh Fish Outlet",
        "offer": {"acceptsItemId": "crystal", "acceptsQuantity": 50,
                  "paysItemId": "fish", "paysQuantity": 1},
        "trade": {"itemId": "crystal", "minQuantity": 50, "batchMultiple": 50},
    }
    cmd = mc._trade_cmd(merchant, merchant["name"])
    assert cmd == "crystal 50 Central Fresh Fish Outlet"
    assert "MC_UNTRUSTED" not in cmd          # must be echoable
    assert not cmd.startswith("fish")         # never the received item

    # Quantity rounds UP to a legal batch multiple.
    m2 = dict(merchant, trade={"itemId": "crystal", "minQuantity": 30,
                               "batchMultiple": 50})
    assert mc._trade_cmd(m2, m2["name"]) == "crystal 50 Central Fresh Fish Outlet"

    # A partial row renders no command rather than a broken one.
    assert mc._trade_cmd({"name": "X", "trade": {}}, "X") is None


def test_trade_inversion_is_corrected(monkeypatch):
    """The model names the item it WANTS, not the one it hands over.

    (mcity-trade "to_go_food 50 Central Mart Outlet")
      -> not enough to_go_food to trade: have 0, need 50
    It repeated that through two separate merchant-listing improvements, so the
    harness now forgives it - the same way it already forgives the
    underscore-shaped argument."""
    terms = {"Central Mart Outlet": {"pays": "to_go_food", "takes": "crystal",
                                     "min": 50, "batch": 50}}
    monkeypatch.setattr(mc, "_merchant_terms", lambda name: terms.get(name))
    sent = {}
    monkeypatch.setattr(mc, "_mutate",
                        lambda verb, build: (sent.update(build()[0] or {}) or "MCITY-TRADE-OK"))
    monkeypatch.setattr(mc, "trade_enabled", lambda: True)
    monkeypatch.setattr(mc, "_c", lambda k, d=None:
                        100000 if k == "trade_max_quantity" else ("*",) if k == "trade_merchants" else d)

    out = mc.trade("to_go_food 50 Central Mart Outlet")
    assert sent["itemId"] == "crystal"          # corrected to what we hand over
    assert sent["quantity"] == 50
    assert "_corrected_from" not in sent        # never sent to the world
    assert "corrected=" in out                  # but reported to the agent

    # A correct argument is passed through untouched.
    sent.clear()
    out = mc.trade("crystal 50 Central Mart Outlet")
    assert sent["itemId"] == "crystal"
    assert "corrected=" not in out


def test_vitals_ride_along_on_every_result(monkeypatch):
    """The agent spent 68 of 75 reads in ten minutes re-asking its own hunger
    and inventory. Carrying the answer on every result removes the reason."""
    mc._VITALS.update({"at_ms": 0, "hunger": None, "space": None, "items": None})
    assert mc._vitals_line() is None                 # nothing known yet

    mc._harvest_vitals({
        "agent": {"position": {"spaceId": "central"}},
        "hunger": {"state": "starving", "value": 100},
        "inventory": {"crystal": 13950, "fish": 1},
    })
    line = mc._vitals_line()
    assert "hunger=starving" in line
    assert "at=central" in line
    assert "crystal=13950" in line

    out = mc._out("MCITY-NEEDS-OK")
    assert out.startswith("MCITY-NEEDS-OK")
    assert "vitals hunger=starving" in out
    # Never doubled when a caller already appended one.
    assert mc._out(out).count("vitals hunger=") == 1


def test_harvest_never_raises_on_junk():
    for junk in (None, [], "text", {"agent": "not-a-dict"}, {"hunger": 5}):
        mc._harvest_vitals(junk)                     # must not raise


def test_vitals_survive_truncation(monkeypatch):
    """_cap truncates the tail, so appending vitals before capping would chop
    them off exactly on the long results that most need grounding."""
    monkeypatch.setattr(mc, "_c", lambda k, d=None: 200 if k == "max_result_chars" else d)
    mc._VITALS.update({"at_ms": mc._now_ms(), "hunger": "starving",
                       "space": "central", "items": "crystal=1"})
    out = mc._out("X" * 5000)
    assert out.endswith(mc._vitals_line())     # vitals kept
    assert "...TRUNCATED" in out               # body was the thing cut
    assert len(out) <= 200 + len(mc._vitals_line()) + 20


def test_eat_is_refused_when_not_hungry(monkeypatch):
    """Once fed, the agent issued eat 88 times in ten minutes against
    'agent is not hungry', and the holding= hint made it worse by confirming it
    still had food. Eating when not hungry cannot succeed, so it is refused from
    our own vitals without a world call."""
    called = []
    monkeypatch.setattr(mc, "_mutate", lambda verb, build: called.append(verb) or "MCITY-EAT-OK")
    mc._VITALS.update({"at_ms": mc._now_ms(), "hunger": "normal(1)",
                       "space": "central", "items": "to_go_food=3", "status": "idle"})
    out = mc.eat()
    assert "not_hungry" in out
    assert called == []                       # no world call at all

    # Starving still goes through to the world.
    mc._VITALS["hunger"] = "starving(100)"
    mc.eat()
    assert called == ["EAT"]

    # Stale vitals must not block a legitimate attempt.
    called.clear()
    mc._VITALS.update({"hunger": "normal(1)",
                       "at_ms": mc._now_ms() - (mc._VITALS_STALE_MS + 1000)})
    mc.eat()
    assert called == ["EAT"]


def test_speak_failure_offers_reachable_targets(monkeypatch):
    """A speak failure was a dead end: the agent held one id from an old thread,
    the world said 'target is sleeping', and it retried the same sleeping target
    six times because nothing told it who else was there."""
    payload = {"agents": [
        {"id": "user-agent-awake", "name": "Masha", "isOpenToTalk": True,
         "canSpeak": True, "isOnSameMap": True, "distance": 3, "status": "idle"},
        {"id": "user-agent-far", "name": "Zed", "isOpenToTalk": True,
         "canSpeak": True, "isOnSameMap": True, "distance": 40, "status": "idle"},
        {"id": "user-agent-shut", "name": "Sleeper", "isOpenToTalk": False,
         "canSpeak": False, "isOnSameMap": True, "distance": 1, "status": "sleeping"},
    ]}
    monkeypatch.setattr(mc, "_skill_read", lambda verb, ep: (payload, None))
    monkeypatch.setattr(mc, "_store_call", lambda op, default=None: (default, True))
    out = mc._speak_candidates()
    assert "user-agent-awake" in out
    assert "user-agent-shut" not in out          # not open to talk
    assert out.index("user-agent-awake") < out.index("user-agent-far")  # nearest first
    assert "MC_UNTRUSTED" not in out             # must stay copy-ready


def test_speak_candidates_never_leak_quarantined_names(monkeypatch):
    payload = {"agents": [{"id": "user-agent-x", "name": "Two Words",
                           "isOpenToTalk": True, "canSpeak": True,
                           "isOnSameMap": True, "distance": 1, "status": "idle"}]}
    monkeypatch.setattr(mc, "_skill_read", lambda verb, ep: (payload, None))
    monkeypatch.setattr(mc, "_store_call", lambda op, default=None: (default, True))
    out = mc._speak_candidates()
    assert "user-agent-x" in out
    assert "MC_UNTRUSTED" not in out             # name dropped, id kept


def test_store_config_reads_the_environment(monkeypatch):
    """_config reads OmegaClaw's config, NOT the environment, so every
    OMEGACLAW_PG_* variable entrypoint.sh forwards was inert: the store resolved
    to 127.0.0.1:5433 and failed with 'no password supplied' while the agent ran
    on the in-memory fallback."""
    monkeypatch.setattr(mc, "_config", lambda key, default: default)
    monkeypatch.setenv("OMEGACLAW_PG_HOST", "/var/run/postgresql")
    monkeypatch.setenv("OMEGACLAW_PG_PORT", "5432")
    monkeypatch.setenv("OMEGACLAW_PG_DB", "omegaclaw")
    monkeypatch.setenv("OMEGACLAW_PG_USER", "nobody")
    cfg = mc._resolve_config("http://localhost:8080", "user-agent-x", "control")
    assert cfg["pg_host"] == "/var/run/postgresql"   # a socket dir, not an address
    assert cfg["pg_port"] == 5432
    assert cfg["pg_user"] == "nobody"                # unprivileged role, not the owner
    assert cfg["pg_dbname"] == "omegaclaw"


def test_degraded_store_promotes_itself_back(monkeypatch):
    """Falling back used to be permanent: a database that was merely late cost
    the roster until the next restart, while a healthy Postgres sat beside it."""
    import itertools
    health = itertools.chain([False], itertools.repeat(True))

    class Fake:
        def __init__(self, cfg): self.cfg = cfg
        def health(self): return next(health) if self.cfg.get("backend") != "memory" else True

    monkeypatch.setattr(mc, "make_store", lambda cfg: Fake(cfg))
    monkeypatch.setattr(mc, "_store_settings", lambda backend: {"backend": backend})
    monkeypatch.setattr(mc, "_c", lambda k, d=None: "postgres" if k == "memory_backend" else d)
    mc._store = None; mc._store_ready = False
    mc._store_degraded = False; mc._store_rebuild_at = 0.0

    first = mc._roster_store()                     # health() False -> fallback
    assert mc._store_degraded is True
    assert first.cfg["backend"] == "memory"

    # Inside the rebuild window nothing is retried.
    mc._store_rebuild_at = mc.time.monotonic() + 9999
    assert mc._roster_store().cfg["backend"] == "memory"
    assert mc._store_degraded is True

    # Once the window passes it promotes itself back.
    mc._store_rebuild_at = 0.0
    promoted = mc._roster_store()
    assert promoted.cfg["backend"] == "postgres"
    assert mc._store_degraded is False
