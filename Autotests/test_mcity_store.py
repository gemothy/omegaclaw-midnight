"""Offline tests for plugins/mcity/mcity_store — the RosterStore contract.

docs/ARCHITECTURE-memory.md is the binding contract. Everything here runs
WITHOUT a live database by exercising `InMemoryStore`, which exists for
exactly this purpose and must behave observably like `PostgresStore`. The
PostgresStore mirror tests at the bottom run the SAME ranking scenario and
skip cleanly when psycopg (v3) or the database is unavailable — psycopg is
absent on most dev hosts (the image gets it from apt as python3-psycopg),
so a skip there is expected, not a failure.

What must never regress:

  * upsert deduplicates on agent_id and merges (None never clobbers),
  * mark_spoken increments spoke_count — the anti-greeting-loop counter,
  * candidates() ranks by is_talking_to_you DESC (a hard first key: someone
    already addressing us always wins), then spoke_count ASC,
    last_spoken_ms ASC, dist ASC (unknown dist last),
  * candidates() excludes agents inside the cooldown window (strictly:
    spoken exactly at the boundary is still inside) and agents that are not
    eligible (can_speak AND is_on_same_map AND (is_open_to_talk OR
    is_talking_to_you)); `status` is stored but no longer filtered on.

Run:
    python3 -m pytest Autotests/test_mcity_store.py -q
"""
import dataclasses
import os
import sys
import threading
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_PLUGIN = os.path.join(_REPO, "plugins", "mcity")
if _PLUGIN not in sys.path:
    sys.path.insert(0, _PLUGIN)

from mcity_store import (                                   # noqa: E402
    AgentObservation, AgentRow, RosterStore, make_store,
)
from mcity_store.memory import InMemoryStore                # noqa: E402
from mcity_store.postgres import PostgresStore              # noqa: E402

try:
    import psycopg                                          # noqa: F401
    _HAVE_PSYCOPG = True
except ImportError:
    _HAVE_PSYCOPG = False

requires_psycopg = pytest.mark.skipif(
    not _HAVE_PSYCOPG, reason="psycopg (v3) is not installed on this host")

NOW = 1_754_800_000_000          # a realistic wall clock, in ms
COOLDOWN = 600_000               # 10 minutes
CUTOFF = NOW - COOLDOWN
T0 = NOW - COOLDOWN * 6          # spoken long ago
T1 = NOW - COOLDOWN * 4
T2 = NOW - COOLDOWN * 2


def _seen(agent_id, *, dist=None, open_to_talk=True, talking=False,
          can_speak=True, same_map=True, status="idle", name=None,
          profession=None, at=NOW, thread_id=None):
    """A fully eligible observation unless a keyword says otherwise."""
    return AgentObservation(
        agent_id=agent_id, name=name, status=status,
        is_open_to_talk=open_to_talk, is_talking_to_you=talking,
        can_speak=can_speak, is_on_same_map=same_map, dist=dist,
        profession=profession, observed_at_ms=at, thread_id=thread_id)


def _ids(rows):
    return [row.agent_id for row in rows]


def _populate_ranking(store):
    """Build the canonical ranking scenario through the public API only, and
    return the exact candidate order the contract demands. Shared by the
    memory and postgres tests so the two backends cannot drift apart."""
    store.upsert_agents([
        # included, in expected rank order (ids chosen so alphabetical
        # order would be WRONG if any sort key were skipped):
        _seen("e-closed-but-talking", dist=40.0, open_to_talk=False,
              talking=True),                       # talking beats everything
        _seen("zz-talking", dist=90.0, talking=True),   # talking, spoken 2x
        _seen("nn-near", dist=5.0),                     # unspoken, nearest
        _seen("ff-far", dist=50.0, status="busy"),      # status NOT filtered
        _seen("uu-unknown-dist", dist=None),            # unknown dist last
        _seen("aa-old-once", dist=70.0),                # spoken once, oldest
        _seen("bb-recent-once", dist=2.0),              # spoken once, newer
        _seen("y-boundary-ok", dist=3.0),               # 1ms outside cooldown
        _seen("cc-twice", dist=1.0),                    # spoken twice
        # excluded, whatever their rank would have been:
        _seen("x-cooldown", dist=1.0),                  # exactly at boundary
        _seen("x-cooldown-talking", dist=1.0, talking=True),
        _seen("x-muted", dist=1.0, can_speak=False),
        _seen("x-elsewhere", dist=1.0, same_map=False),
        _seen("x-closed", dist=1.0, open_to_talk=False),
    ])
    store.mark_spoken("zz-talking", T0, "hello")
    store.mark_spoken("zz-talking", T1, "hello again")
    store.mark_spoken("aa-old-once", T1, "old")
    store.mark_spoken("bb-recent-once", T2, "recent")
    store.mark_spoken("y-boundary-ok", CUTOFF - 1, "just outside")
    store.mark_spoken("cc-twice", NOW - COOLDOWN * 7, "first")
    store.mark_spoken("cc-twice", T0, "second")
    store.mark_spoken("x-cooldown", CUTOFF, "boundary")     # still inside
    store.mark_spoken("x-cooldown-talking", NOW - 1_000, "just now")
    store.mark_spoken("x-unspoken-marked", T0, "never observed")
    return ["e-closed-but-talking",  # talking, spoke_count 0
            "zz-talking",            # talking, spoke_count 2
            "nn-near",               # 0 spokes, dist 5
            "ff-far",                # 0 spokes, dist 50
            "uu-unknown-dist",       # 0 spokes, dist unknown -> last of group
            "aa-old-once",           # 1 spoke at T1
            "bb-recent-once",        # 1 spoke at T2 > T1
            "y-boundary-ok",         # 1 spoke at CUTOFF-1, newest allowed
            "cc-twice"]              # 2 spokes


@pytest.fixture
def store():
    return InMemoryStore()


# --------------------------------------------------------------------------
# contract shape
# --------------------------------------------------------------------------

def test_agent_row_carries_every_contract_field():
    required = {
        # the original roster columns
        "agent_id", "name", "status", "dist", "last_seen_ms",
        "last_spoken_ms", "spoke_count", "thread_id", "last_spoken_text",
        # the observation-schema extension
        "is_open_to_talk", "is_talking_to_you", "can_speak",
        "is_on_same_map", "profession",
    }
    assert {f.name for f in dataclasses.fields(AgentRow)} == required


def test_observation_carries_the_schema_fields():
    required = {"agent_id", "name", "status", "is_open_to_talk",
                "is_talking_to_you", "can_speak", "is_on_same_map", "dist",
                "profession", "observed_at_ms", "thread_id"}
    assert {f.name for f in dataclasses.fields(AgentObservation)} == required


def test_rows_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        AgentRow(agent_id="a").spoke_count = 5
    with pytest.raises(dataclasses.FrozenInstanceError):
        AgentObservation(agent_id="a").dist = 1.0


def test_memory_store_satisfies_the_protocol(store):
    assert isinstance(store, RosterStore)


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------

def test_factory_selects_the_memory_backend():
    built = make_store({"backend": "memory"})
    assert isinstance(built, InMemoryStore)
    assert isinstance(built, RosterStore)


def test_factory_defaults_to_postgres():
    if _HAVE_PSYCOPG:
        # Construction is lazy: no database is contacted here.
        assert isinstance(make_store({}), PostgresStore)
    else:
        with pytest.raises(ImportError, match="python3-psycopg"):
            make_store({})


def test_factory_rejects_unknown_and_sqlite_backends():
    with pytest.raises(ValueError, match="unknown roster store backend"):
        make_store({"backend": "flatfile"})
    with pytest.raises(ValueError, match="sqlite"):
        make_store({"backend": "sqlite"})


@pytest.mark.skipif(_HAVE_PSYCOPG,
                    reason="psycopg is installed; this path is unreachable")
def test_postgres_without_psycopg_names_the_apt_package():
    with pytest.raises(ImportError, match="python3-psycopg"):
        PostgresStore({})


@requires_psycopg
def test_postgres_rejects_a_hostile_table_name():
    with pytest.raises(ValueError, match="table name"):
        PostgresStore({"table": 'roster"; DROP TABLE agents; --'})


# --------------------------------------------------------------------------
# upsert: dedupe and merge
# --------------------------------------------------------------------------

def test_upsert_dedupes_on_agent_id(store):
    store.upsert_agents([
        _seen("gem", name="Gem", dist=10.0),
        _seen("gem", name="Gem", dist=12.0),        # same batch
    ])
    store.upsert_agents([_seen("gem", name="Gem", dist=8.0)])  # next batch
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10)
    assert _ids(rows) == ["gem"]
    assert rows[0].dist == 8.0                      # last write won


def test_upsert_none_fields_keep_stored_values(store):
    store.upsert_agents([_seen("gem", name="Gem", dist=10.0,
                               profession="miner", thread_id="th-1")])
    # A partial source (for example `context`) states only the status.
    store.upsert_agents([AgentObservation(agent_id="gem", status="busy")])
    row = store.get("gem")
    assert row.status == "busy"
    assert row.name == "Gem"
    assert row.dist == 10.0
    assert row.profession == "miner"
    assert row.thread_id == "th-1"
    assert row.is_open_to_talk and row.can_speak and row.is_on_same_map


def test_upsert_false_is_a_statement_not_a_gap(store):
    store.upsert_agents([_seen("gem")])
    store.upsert_agents([AgentObservation(agent_id="gem",
                                          is_open_to_talk=False)])
    row = store.get("gem")
    assert row.is_open_to_talk is False             # explicitly updated
    assert row.can_speak is True                    # untouched by None


def test_upsert_last_seen_is_monotonic(store):
    store.upsert_agents([_seen("gem", at=NOW)])
    store.upsert_agents([_seen("gem", at=NOW - 5_000)])   # stale batch
    assert store.get("gem").last_seen_ms == NOW


def test_upsert_skips_rows_without_an_agent_id(store):
    store.upsert_agents([AgentObservation(agent_id=""), _seen("gem")])
    assert store.get("") is None
    assert store.get("gem") is not None


def test_upsert_never_touches_spoken_bookkeeping(store):
    store.upsert_agents([_seen("gem")])
    store.mark_spoken("gem", T0, "hi")
    store.upsert_agents([_seen("gem", dist=3.0)])
    row = store.get("gem")
    assert row.spoke_count == 1
    assert row.last_spoken_ms == T0
    assert row.last_spoken_text == "hi"


# --------------------------------------------------------------------------
# mark_spoken
# --------------------------------------------------------------------------

def test_mark_spoken_increments_spoke_count(store):
    store.upsert_agents([_seen("gem", name="Gem", dist=4.0)])
    store.mark_spoken("gem", T0, "first hello")
    store.mark_spoken("gem", T1, "second hello")
    row = store.get("gem")
    assert row.spoke_count == 2
    assert row.last_spoken_ms == T1
    assert row.last_spoken_text == "second hello"
    assert row.name == "Gem" and row.dist == 4.0    # observation intact


def test_mark_spoken_creates_an_unobserved_agent_row(store):
    store.mark_spoken("stranger", T0, "hello?")
    row = store.get("stranger")
    assert row is not None
    assert row.spoke_count == 1
    assert row.last_spoken_ms == T0
    # Never positively observed: not eligible, whatever the cooldown says.
    assert not (row.can_speak or row.is_on_same_map or row.is_open_to_talk)
    assert store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10) == []


def test_mark_spoken_without_an_agent_id_is_a_noop(store):
    store.mark_spoken("", NOW, "into the void")
    assert store.get("") is None


def test_mark_spoken_counts_exactly_under_concurrency(store):
    store.upsert_agents([_seen("gem")])

    def hammer():
        for _ in range(50):
            store.mark_spoken("gem", T0, "hi")
            store.upsert_agents([_seen("gem", dist=2.0)])

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.get("gem").spoke_count == 200


# --------------------------------------------------------------------------
# candidates: ranking
# --------------------------------------------------------------------------

def test_candidates_full_contract_ranking(store):
    expected = _populate_ranking(store)
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=50)
    assert _ids(rows) == expected


def test_talking_to_you_outranks_an_unspoken_nearer_agent(store):
    # The key the original spec missed: someone already addressing us wins
    # even against a never-spoken, much closer agent.
    store.upsert_agents([
        _seen("chatty", dist=90.0, talking=True),
        _seen("fresh", dist=1.0),
    ])
    store.mark_spoken("chatty", T0, "hi")
    store.mark_spoken("chatty", T1, "hi again")     # spoke_count 2
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10)
    assert _ids(rows) == ["chatty", "fresh"]


def test_never_spoken_sorts_before_spoken(store):
    store.upsert_agents([_seen("spoken", dist=1.0), _seen("fresh", dist=99.0)])
    store.mark_spoken("spoken", T0, "hello")
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10)
    assert _ids(rows) == ["fresh", "spoken"]


def test_unknown_dist_ranks_after_any_known_dist(store):
    store.upsert_agents([_seen("mystery", dist=None),
                         _seen("measured", dist=5_000.0)])
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10)
    assert _ids(rows) == ["measured", "mystery"]


def test_full_ties_break_deterministically_by_agent_id(store):
    store.upsert_agents([_seen("kilo", dist=7.0)])
    store.upsert_agents([_seen("alpha", dist=7.0)])
    store.upsert_agents([_seen("echo", dist=7.0)])
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10)
    assert _ids(rows) == ["alpha", "echo", "kilo"]


def test_candidates_limit_truncates_after_ranking(store):
    expected = _populate_ranking(store)
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=3)
    assert _ids(rows) == expected[:3]
    assert store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=0) == []


# --------------------------------------------------------------------------
# candidates: exclusion
# --------------------------------------------------------------------------

def test_cooldown_boundary_is_strict(store):
    store.upsert_agents([_seen("edge", dist=1.0), _seen("okay", dist=1.0)])
    store.mark_spoken("edge", CUTOFF, "at the boundary")       # inside
    store.mark_spoken("okay", CUTOFF - 1, "one ms earlier")    # outside
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10)
    assert _ids(rows) == ["okay"]


def test_cooldown_excludes_even_a_talking_agent(store):
    # The filter is conjunctive by contract: is_talking_to_you changes the
    # RANK, never the cooldown.
    store.upsert_agents([_seen("eager", dist=1.0, talking=True)])
    store.mark_spoken("eager", NOW - 1_000, "just spoke")
    assert store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10) == []


def test_eligibility_gates_exclude(store):
    store.upsert_agents([
        _seen("muted", can_speak=False),
        _seen("elsewhere", same_map=False),
        _seen("closed", open_to_talk=False),
        _seen("welcoming"),
    ])
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10)
    assert _ids(rows) == ["welcoming"]


def test_talking_to_you_overrides_a_closed_door(store):
    # (is_open_to_talk OR is_talking_to_you): someone already addressing us
    # is a candidate even if they do not advertise openness.
    store.upsert_agents([_seen("busy-but-talking", open_to_talk=False,
                               talking=True)])
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10)
    assert _ids(rows) == ["busy-but-talking"]


def test_status_is_stored_but_not_filtered(store):
    # is_open_to_talk supersedes the old status='idle' check; the column
    # stays because the prompt references it.
    store.upsert_agents([_seen("worker", status="busy")])
    rows = store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10)
    assert _ids(rows) == ["worker"]
    assert rows[0].status == "busy"


# --------------------------------------------------------------------------
# get / health
# --------------------------------------------------------------------------

def test_get_unknown_agent_returns_none(store):
    assert store.get("nobody") is None


def test_memory_health_is_true(store):
    assert store.health() is True


# --------------------------------------------------------------------------
# PostgresStore mirror (skips without psycopg or without the database)
# --------------------------------------------------------------------------

def _pg_password():
    value = os.environ.get("OMEGACLAW_PG_PASSWORD")
    if value:
        return value
    path = os.path.expanduser("~/.config/omegaclaw/memory.env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("OMEGACLAW_PG_PASSWORD="):
                    return stripped.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return None


def _pg_config(table):
    password = _pg_password()
    if password is None:
        return None
    return {"backend": "postgres",
            "host": os.environ.get("OMEGACLAW_PG_HOST", "127.0.0.1"),
            "port": int(os.environ.get("OMEGACLAW_PG_PORT", "5433")),
            "dbname": os.environ.get("OMEGACLAW_PG_DBNAME", "omegaclaw"),
            "user": os.environ.get("OMEGACLAW_PG_USER", "omegaclaw"),
            "password": password,
            "table": table,
            "timeout": 5.0}


def _pg_drop(cfg):
    try:
        with psycopg.connect(host=cfg["host"], port=cfg["port"],
                             dbname=cfg["dbname"], user=cfg["user"],
                             password=cfg["password"], connect_timeout=5,
                             autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP TABLE IF EXISTS {}").format(
                psycopg.sql.Identifier(cfg["table"])))
    except Exception:
        pass                            # cleanup is best-effort


@pytest.fixture
def pg_store():
    if not _HAVE_PSYCOPG:
        pytest.skip("psycopg (v3) is not installed on this host")
    table = f"mcity_roster_test_{uuid.uuid4().hex[:12]}"
    cfg = _pg_config(table)
    if cfg is None:
        pytest.skip("no OMEGACLAW_PG_PASSWORD in the environment or "
                    "~/.config/omegaclaw/memory.env")
    built = PostgresStore(cfg)
    if not built.health():
        built.close()
        pytest.skip(f"postgres is not reachable at "
                    f"{cfg['host']}:{cfg['port']}/{cfg['dbname']}")
    yield built
    built.close()
    _pg_drop(cfg)


def test_pg_satisfies_the_protocol_and_schema_is_idempotent(pg_store):
    assert isinstance(pg_store, RosterStore)
    # A second store on the same table re-runs CREATE ... IF NOT EXISTS.
    twin = PostgresStore({**_pg_config(pg_store._table)})
    try:
        assert twin.health() is True
        twin.upsert_agents([_seen("gem", name="Gem")])
        assert pg_store.get("gem").name == "Gem"    # same table, both live
    finally:
        twin.close()


def test_pg_upsert_dedupe_and_merge(pg_store):
    pg_store.upsert_agents([_seen("gem", name="Gem", dist=10.0),
                            _seen("gem", name="Gem", dist=12.0)])
    pg_store.upsert_agents([AgentObservation(agent_id="gem", status="busy")])
    pg_store.upsert_agents([_seen("gem", at=NOW - 5_000)])    # stale batch
    row = pg_store.get("gem")
    assert row.dist == 12.0                         # deduped, last write won
    assert row.status == "idle"                     # refreshed by the batch
    assert row.name == "Gem"                        # None never clobbered
    assert row.last_seen_ms == NOW                  # monotonic
    rows = pg_store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=10)
    assert _ids(rows) == ["gem"]


def test_pg_mark_spoken_increments_and_creates(pg_store):
    pg_store.upsert_agents([_seen("gem")])
    pg_store.mark_spoken("gem", T0, "first")
    pg_store.mark_spoken("gem", T1, "second")
    row = pg_store.get("gem")
    assert row.spoke_count == 2
    assert row.last_spoken_ms == T1
    assert row.last_spoken_text == "second"
    pg_store.mark_spoken("stranger", T0, "hello?")
    stranger = pg_store.get("stranger")
    assert stranger.spoke_count == 1
    assert not stranger.can_speak                   # never observed


def test_pg_candidates_full_contract_ranking(pg_store):
    expected = _populate_ranking(pg_store)
    rows = pg_store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=50)
    assert _ids(rows) == expected
    top = pg_store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN, limit=3)
    assert _ids(top) == expected[:3]
    assert pg_store.candidates(now_ms=NOW, cooldown_ms=COOLDOWN,
                               limit=0) == []


def test_pg_concurrent_writers_never_lose_a_spoke(pg_store):
    # Two stores = two connections = two sessions racing ON CONFLICT rows,
    # which is exactly the concurrent-writer situation the architecture
    # requires to be safe.
    pg_store.upsert_agents([_seen("gem")])
    twin = PostgresStore({**_pg_config(pg_store._table)})

    def hammer(target):
        for _ in range(20):
            target.mark_spoken("gem", T0, "hi")

    try:
        threads = [threading.Thread(target=hammer, args=(pg_store,)),
                   threading.Thread(target=hammer, args=(twin,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert pg_store.get("gem").spoke_count == 40
    finally:
        twin.close()
