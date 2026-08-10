"""PostgreSQL `RosterStore` — the shipped default backend.

Runs against the dedicated pgvector container (`pgvector/pgvector:pg16`,
port 5433). Never point it at `nexus-postgres` on 5432: that database holds
unrelated data and lacks the `vector` extension
(docs/ARCHITECTURE-memory.md, "Backends").

Design notes, in the order they matter:

  * psycopg (v3) is the only non-stdlib dependency and is imported softly,
    so this module always imports (the factory and the memory backend must
    work without it); construction fails instead, with the install line.
    The base image has no pip — psycopg comes from apt (3.1.x), so nothing
    here may rely on the psycopg 3.2+ API.
  * the schema is created idempotently on first use (CREATE TABLE/INDEX IF
    NOT EXISTS), and the `vector` extension is enabled defensively for the
    semantic tables that share this database — but the roster itself stores
    NO embedding column. "Who have I not spoken to" is a filter/sort
    problem; putting vectors here is an architecture non-goal.
  * every write is a single `INSERT ... ON CONFLICT DO UPDATE` statement on
    an autocommit connection, so concurrent writers (a second container,
    another harness) serialise per row inside the server instead of racing
    a read-modify-write here. A batch upsert runs inside one transaction so
    a roster refresh is all-or-nothing.
  * every call is bounded: `connect_timeout` on the handshake and a
    server-side `statement_timeout` on every statement, because a hung
    database must not hang the agent loop (docs/ARCHITECTURE-memory.md,
    "Degradation"). Falling back when this store is down is the caller's
    job — `mcity_store.make_store` and the integration own that policy.
  * one connection per store, guarded by a lock, re-opened once when a call
    finds it dead: the postgres container restarting must not wedge the
    agent for the rest of its life.

Configuration keys (all optional): host, port, dbname, user, password,
table, timeout. `table` exists for test isolation and namespacing and is
validated against a strict identifier pattern before it is ever composed
into SQL.
"""

import logging
import re
import threading

try:
    import psycopg
    from psycopg import sql as _sql
except ImportError:                     # the memory backend must still work
    psycopg = None
    _sql = None

try:                                    # inside the agent (src/ on sys.path)
    from src.logger import get_logger
except ModuleNotFoundError:             # the plugin folder is on sys.path
    try:
        from logger import get_logger
    except ModuleNotFoundError:         # offline unit tests

        def get_logger(name):
            return logging.getLogger(name)


from mcity_store.base import (
    DEFAULT_NAME,
    DEFAULT_PROFESSION,
    DEFAULT_STATUS,
    AgentObservation,
    AgentRow,
    _coerce_dist,
    _coerce_flag,
    _coerce_ms,
)

logger = get_logger("mcity.store")


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5433                     # the dedicated pgvector container
DEFAULT_DBNAME = "omegaclaw"
DEFAULT_USER = "omegaclaw"
DEFAULT_TABLE = "mcity_roster"
DEFAULT_TIMEOUT_S = 10.0                # connect AND per-statement budget

INSTALL_HINT = "apt-get install -y --no-install-recommends python3-psycopg"

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_COLUMNS = ("agent_id, name, status, profession, "
            "is_open_to_talk, is_talking_to_you, can_speak, is_on_same_map, "
            "dist, last_seen_ms, last_spoken_ms, spoke_count, thread_id, "
            "last_spoken_text")

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {{table}} (
    agent_id          TEXT PRIMARY KEY,
    name              TEXT NOT NULL DEFAULT '{DEFAULT_NAME}',
    status            TEXT NOT NULL DEFAULT '{DEFAULT_STATUS}',
    profession        TEXT NOT NULL DEFAULT '{DEFAULT_PROFESSION}',
    is_open_to_talk   BOOLEAN NOT NULL DEFAULT FALSE,
    is_talking_to_you BOOLEAN NOT NULL DEFAULT FALSE,
    can_speak         BOOLEAN NOT NULL DEFAULT FALSE,
    is_on_same_map    BOOLEAN NOT NULL DEFAULT FALSE,
    dist              DOUBLE PRECISION,
    last_seen_ms      BIGINT NOT NULL DEFAULT 0,
    last_spoken_ms    BIGINT NOT NULL DEFAULT 0,
    spoke_count       BIGINT NOT NULL DEFAULT 0,
    thread_id         TEXT,
    last_spoken_text  TEXT NOT NULL DEFAULT ''
)
"""

# Matches the ORDER BY prefix of the candidates query; at roster scale (a few
# hundred rows) this is hygiene, not a requirement.
_CREATE_INDEX = ("CREATE INDEX IF NOT EXISTS {index} ON {table} "
                 "(is_talking_to_you DESC, spoke_count, last_spoken_ms, dist)")

# Mirrors mcity_store.base._eligible / _rank_key EXACTLY; change them only
# together. NULLS LAST is the Postgres default for ASC but is spelled out so
# the parity with the in-memory float("inf") convention is visible.
_CANDIDATES = f"""
SELECT {_COLUMNS}
  FROM {{table}}
 WHERE can_speak
   AND is_on_same_map
   AND (is_open_to_talk OR is_talking_to_you)
   AND last_spoken_ms < %(cutoff)s
 ORDER BY is_talking_to_you DESC,
          spoke_count ASC,
          last_spoken_ms ASC,
          dist ASC NULLS LAST,
          agent_id ASC
 LIMIT %(limit)s
"""

_GET = f"SELECT {_COLUMNS} FROM {{table}} WHERE agent_id = %(agent_id)s"

# None parameters mean "the observation did not state it": COALESCE keeps the
# stored value on update and falls back to the column default on insert.
# last_seen_ms is monotonic under GREATEST, so an out-of-order batch from a
# concurrent writer can never rewind it.
_UPSERT = f"""
INSERT INTO {{table}} AS r
       (agent_id, name, status, profession,
        is_open_to_talk, is_talking_to_you, can_speak, is_on_same_map,
        dist, last_seen_ms, thread_id)
VALUES (%(agent_id)s,
        COALESCE(%(name)s, '{DEFAULT_NAME}'),
        COALESCE(%(status)s, '{DEFAULT_STATUS}'),
        COALESCE(%(profession)s, '{DEFAULT_PROFESSION}'),
        COALESCE(%(is_open_to_talk)s, FALSE),
        COALESCE(%(is_talking_to_you)s, FALSE),
        COALESCE(%(can_speak)s, FALSE),
        COALESCE(%(is_on_same_map)s, FALSE),
        %(dist)s,
        %(observed_at_ms)s,
        %(thread_id)s)
ON CONFLICT (agent_id) DO UPDATE SET
       name              = COALESCE(%(name)s, r.name),
       status            = COALESCE(%(status)s, r.status),
       profession        = COALESCE(%(profession)s, r.profession),
       is_open_to_talk   = COALESCE(%(is_open_to_talk)s, r.is_open_to_talk),
       is_talking_to_you = COALESCE(%(is_talking_to_you)s, r.is_talking_to_you),
       can_speak         = COALESCE(%(can_speak)s, r.can_speak),
       is_on_same_map    = COALESCE(%(is_on_same_map)s, r.is_on_same_map),
       dist              = COALESCE(%(dist)s, r.dist),
       last_seen_ms      = GREATEST(r.last_seen_ms, EXCLUDED.last_seen_ms),
       thread_id         = COALESCE(%(thread_id)s, r.thread_id)
"""

# The spoke-count increment happens inside the statement, under the row lock,
# so two concurrent confirmations both count. A speak to a never-observed
# agent creates the row with every eligibility flag at its FALSE default.
_MARK_SPOKEN = """
INSERT INTO {table} AS r
       (agent_id, last_seen_ms, last_spoken_ms, spoke_count, last_spoken_text)
VALUES (%(agent_id)s, %(at_ms)s, %(at_ms)s, 1, %(text)s)
ON CONFLICT (agent_id) DO UPDATE SET
       last_spoken_ms   = %(at_ms)s,
       spoke_count      = r.spoke_count + 1,
       last_spoken_text = %(text)s
"""


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------

class PostgresStore:
    """Roster on the dedicated pgvector Postgres. The shipped default."""

    def __init__(self, config: dict):
        if psycopg is None:
            raise ImportError(
                "PostgresStore needs psycopg (v3), which is not importable "
                f"here. Install it with: {INSTALL_HINT} "
                "(the base image ships no pip). For tests without a "
                "database use the 'memory' backend instead."
            )
        config = config or {}
        self._table = str(config.get("table") or DEFAULT_TABLE)
        if not _TABLE_RE.match(self._table):
            raise ValueError(
                f"invalid roster table name {self._table!r}: "
                "lowercase letters, digits and underscores only"
            )
        self._timeout = self._seconds(config.get("timeout"), DEFAULT_TIMEOUT_S)
        self._conninfo = {
            "host": str(config.get("host") or DEFAULT_HOST),
            "port": self._int(config.get("port"), DEFAULT_PORT),
            "dbname": str(config.get("dbname") or DEFAULT_DBNAME),
            "user": str(config.get("user") or DEFAULT_USER),
            "password": str(config.get("password") or ""),
            "connect_timeout": max(1, int(self._timeout)),
            "application_name": "omegaclaw-mcity-roster",
            # Server-side per-statement budget: a hung database must not
            # hang the agent loop (ARCHITECTURE-memory.md, "Degradation").
            "options": f"-c statement_timeout={int(self._timeout * 1000)}",
        }
        table = _sql.Identifier(self._table)
        index = _sql.Identifier(f"{self._table}_rank_idx")
        self._q_create_table = _sql.SQL(_CREATE_TABLE).format(table=table)
        self._q_create_index = _sql.SQL(_CREATE_INDEX).format(table=table,
                                                              index=index)
        self._q_upsert = _sql.SQL(_UPSERT).format(table=table)
        self._q_mark_spoken = _sql.SQL(_MARK_SPOKEN).format(table=table)
        self._q_candidates = _sql.SQL(_CANDIDATES).format(table=table)
        self._q_get = _sql.SQL(_GET).format(table=table)
        self._lock = threading.RLock()
        self._conn = None               # opened lazily on first use

    # ----------------------------------------------------------------------
    # RosterStore protocol
    # ----------------------------------------------------------------------

    def upsert_agents(self, observed) -> None:
        rows = [self._params(obs) for obs in observed]
        rows = [row for row in rows if row is not None]
        if not rows:
            return

        def op(conn):
            # One transaction so a roster refresh lands all-or-nothing;
            # psycopg supports an explicit block on autocommit connections.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.executemany(self._q_upsert, rows)

        self._run(op)

    def mark_spoken(self, agent_id: str, at_ms: int, text: str) -> None:
        key = str(agent_id or "").strip()
        if not key:
            return
        params = {"agent_id": key,
                  "at_ms": _coerce_ms(at_ms),
                  "text": "" if text is None else str(text)}
        self._run(lambda conn: conn.execute(self._q_mark_spoken, params))

    def candidates(self, *, now_ms: int, cooldown_ms: int, limit: int
                   ) -> list[AgentRow]:
        if limit <= 0:
            return []
        params = {"cutoff": int(now_ms) - int(cooldown_ms),
                  "limit": int(limit)}

        def op(conn):
            with conn.cursor() as cur:
                cur.execute(self._q_candidates, params)
                return cur.fetchall()

        return [self._row(record) for record in self._run(op)]

    def get(self, agent_id: str) -> AgentRow | None:
        key = str(agent_id or "").strip()
        if not key:
            return None

        def op(conn):
            with conn.cursor() as cur:
                cur.execute(self._q_get, {"agent_id": key})
                return cur.fetchone()

        record = self._run(op)
        return None if record is None else self._row(record)

    def health(self) -> bool:
        try:
            def op(conn):
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()

            self._run(op)
            return True
        except Exception as e:         # a probe must answer, never raise
            logger.warning(f"mcity roster store health check failed: {e}")
            return False

    # ----------------------------------------------------------------------
    # lifecycle (not part of the protocol)
    # ----------------------------------------------------------------------

    def close(self):
        """Best-effort release of the connection; the store reopens lazily
        if it is used again. Used by tests and operator tooling."""
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ----------------------------------------------------------------------
    # connection management
    # ----------------------------------------------------------------------

    def _run(self, op):
        """Run one operation against the live connection, reconnecting once
        when the connection turns out to be dead (container restart, idle
        disconnect). Anything the retry raises propagates to the caller —
        the integration layer owns the degrade-and-report policy."""
        with self._lock:
            try:
                return op(self._connection())
            except psycopg.OperationalError:
                self._drop()
                return op(self._connection())

    def _connection(self):
        if self._conn is None or self._conn.closed:
            conn = psycopg.connect(**self._conninfo, autocommit=True)
            try:
                self._prepare(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                raise
            self._conn = conn
        return self._conn

    def _drop(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _prepare(self, conn):
        """Idempotent schema, run on every (re)connect — CREATE ... IF NOT
        EXISTS is cheap and a re-created container comes back ready. The
        vector extension is best-effort: the semantic tables sharing this
        database want it, the roster does not need it, and a role without
        the privilege must not lose the roster over it."""
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error as e:
            logger.warning("could not enable the vector extension "
                           f"(the roster works without it): {e}")
        conn.execute(self._q_create_table)
        conn.execute(self._q_create_index)

    # ----------------------------------------------------------------------
    # small helpers
    # ----------------------------------------------------------------------

    @staticmethod
    def _int(value, default):
        try:
            if value is None or isinstance(value, bool):
                return int(default)
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _seconds(value, default):
        try:
            if value is None or isinstance(value, bool):
                return float(default)
            number = float(value)
        except (TypeError, ValueError):
            return float(default)
        if number != number or number <= 0 or number == float("inf"):
            return float(default)
        return min(number, 600.0)

    @staticmethod
    def _params(obs: AgentObservation):
        """Observation -> named parameters for _UPSERT, None to skip."""
        agent_id = str(obs.agent_id or "").strip()
        if not agent_id:
            return None                 # a row without a key is unusable
        return {
            "agent_id": agent_id,
            "name": None if obs.name is None else str(obs.name),
            "status": None if obs.status is None else str(obs.status),
            "profession": (None if obs.profession is None
                           else str(obs.profession)),
            "is_open_to_talk": _coerce_flag(obs.is_open_to_talk),
            "is_talking_to_you": _coerce_flag(obs.is_talking_to_you),
            "can_speak": _coerce_flag(obs.can_speak),
            "is_on_same_map": _coerce_flag(obs.is_on_same_map),
            "dist": _coerce_dist(obs.dist),
            "observed_at_ms": _coerce_ms(obs.observed_at_ms),
            "thread_id": None if obs.thread_id is None else str(obs.thread_id),
        }

    @staticmethod
    def _row(record):
        """One SELECT record (in _COLUMNS order) -> AgentRow."""
        (agent_id, name, status, profession, is_open_to_talk,
         is_talking_to_you, can_speak, is_on_same_map, dist, last_seen_ms,
         last_spoken_ms, spoke_count, thread_id, last_spoken_text) = record
        return AgentRow(
            agent_id=agent_id,
            name=DEFAULT_NAME if name is None else name,
            status=DEFAULT_STATUS if status is None else status,
            profession=(DEFAULT_PROFESSION if profession is None
                        else profession),
            is_open_to_talk=bool(is_open_to_talk),
            is_talking_to_you=bool(is_talking_to_you),
            can_speak=bool(can_speak),
            is_on_same_map=bool(is_on_same_map),
            dist=None if dist is None else float(dist),
            last_seen_ms=int(last_seen_ms or 0),
            last_spoken_ms=int(last_spoken_ms or 0),
            spoke_count=int(spoke_count or 0),
            thread_id=thread_id,
            last_spoken_text="" if last_spoken_text is None
                             else last_spoken_text,
        )
