"""Roster store for the `mcity` plugin — structured world state.

docs/ARCHITECTURE-memory.md is the binding contract. This package is the
`RosterStore` half of the grounded-memory fix: a queryable, relevance-ranked
roster replacing the `text[:2000]` truncation that dropped 98.8% of a
284-agent listing. The semantic layer (Chroma) is a different question and a
different store; nothing here embeds anything.

The plugin directory is appended last on `sys.path` and modules load
top-level, so this package is deliberately namespaced (`mcity_store`, never
`store`) and uses absolute imports throughout — a generic name would be
silently shadowed by any installed package, inside an unattended agent loop.

Usage:

    from mcity_store import make_store
    store = make_store({"backend": "postgres", "host": "127.0.0.1",
                        "port": 5433, "dbname": "omegaclaw",
                        "user": "omegaclaw", "password": secret})

Backends (docs/ARCHITECTURE-memory.md, "Backends"):

    postgres — the shipped default: the dedicated pgvector container on
               port 5433, safe under concurrent writers.
    memory   — tests only, never a deployment target.

There is deliberately no SQLite backend: single-writer and no network
access make it unfit for a multi-agent framework.
"""

from mcity_store.base import AgentObservation, AgentRow, RosterStore

__all__ = ["AgentObservation", "AgentRow", "RosterStore", "make_store"]


def make_store(config: dict) -> RosterStore:
    """Build the roster store selected by `config["backend"]`.

    The concrete backend is imported lazily so a host without psycopg can
    still build the memory backend, and a missing psycopg surfaces as the
    PostgresStore ImportError (with the install line) rather than as an
    import failure of this package. Falling back to `memory` when postgres
    is unreachable is the CALLER's decision, made on `health()`, and must
    be reported, never silent (ARCHITECTURE-memory.md, "Degradation")."""
    config = config or {}
    backend = str(config.get("backend") or "postgres").strip().lower()
    if backend == "postgres":
        from mcity_store.postgres import PostgresStore
        return PostgresStore(config)
    if backend == "memory":
        from mcity_store.memory import InMemoryStore
        return InMemoryStore()
    if backend == "sqlite":
        raise ValueError(
            "sqlite is not a roster backend: single-writer and no network "
            "access make it unfit for a multi-agent framework "
            "(docs/ARCHITECTURE-memory.md); use 'postgres', or 'memory' "
            "in tests"
        )
    raise ValueError(f"unknown roster store backend {backend!r}; "
                     "use 'postgres' or 'memory'")
