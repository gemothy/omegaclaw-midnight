"""In-memory `RosterStore` — tests only, never a deployment target.

docs/ARCHITECTURE-memory.md lists this backend for exactly one job: letting
the roster semantics be tested without a live database. It must therefore
behave observably like `mcity_store.postgres.PostgresStore` — same merge
rules, same ranking, same filter — which is why every semantic decision
lives in `mcity_store.base` helpers shared by both, instead of being written
twice. It holds no durability, no cross-process visibility and no vector
extension, so it can never be the shipped default.

Thread safety: a single lock around the dict. The Postgres backend gets the
equivalent guarantee from single-statement `INSERT ... ON CONFLICT DO
UPDATE`, and the tests hammer both the same way.

Only the Python standard library is used.
"""

import threading
from collections.abc import Sequence
from dataclasses import replace

from mcity_store.base import (
    AgentObservation,
    AgentRow,
    _coerce_dist,
    _coerce_flag,
    _coerce_ms,
    _eligible,
    _rank_key,
)


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------

class InMemoryStore:
    """Dict-backed roster with `PostgresStore` semantics. Tests only."""

    def __init__(self):
        self._lock = threading.RLock()
        self._rows = {}     # agent_id -> AgentRow

    # ----------------------------------------------------------------------
    # RosterStore protocol
    # ----------------------------------------------------------------------

    def upsert_agents(self, observed: Sequence[AgentObservation]) -> None:
        with self._lock:
            for obs in observed:
                agent_id = self._key(obs.agent_id)
                if not agent_id:
                    continue    # a row without a key cannot be upserted
                row = self._rows.get(agent_id) or AgentRow(agent_id=agent_id)
                self._rows[agent_id] = self._merge(row, obs)

    def mark_spoken(self, agent_id: str, at_ms: int, text: str) -> None:
        key = self._key(agent_id)
        if not key:
            return
        at = _coerce_ms(at_ms)
        body = "" if text is None else str(text)
        with self._lock:
            row = self._rows.get(key) or AgentRow(agent_id=key, last_seen_ms=at)
            self._rows[key] = replace(row,
                                      last_spoken_ms=at,
                                      spoke_count=row.spoke_count + 1,
                                      last_spoken_text=body)

    def candidates(self, *, now_ms: int, cooldown_ms: int, limit: int
                   ) -> list[AgentRow]:
        if limit <= 0:
            return []
        cutoff = int(now_ms) - int(cooldown_ms)
        with self._lock:
            rows = [row for row in self._rows.values() if _eligible(row, cutoff)]
        rows.sort(key=_rank_key)
        return rows[:limit]

    def get(self, agent_id: str) -> AgentRow | None:
        with self._lock:
            return self._rows.get(self._key(agent_id))

    def health(self) -> bool:
        return True

    # ----------------------------------------------------------------------
    # helpers
    # ----------------------------------------------------------------------

    @staticmethod
    def _key(agent_id):
        try:
            return str(agent_id or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _merge(row, obs):
        """One observation folded into one row: None keeps the stored value,
        last_seen_ms is monotonic, spoken bookkeeping is never touched.
        Mirrors the COALESCE/GREATEST clauses of the Postgres upsert."""
        dist = _coerce_dist(obs.dist)
        open_to_talk = _coerce_flag(obs.is_open_to_talk)
        talking = _coerce_flag(obs.is_talking_to_you)
        can_speak = _coerce_flag(obs.can_speak)
        same_map = _coerce_flag(obs.is_on_same_map)
        return replace(
            row,
            name=row.name if obs.name is None else str(obs.name),
            status=row.status if obs.status is None else str(obs.status),
            profession=(row.profession if obs.profession is None
                        else str(obs.profession)),
            is_open_to_talk=(row.is_open_to_talk if open_to_talk is None
                             else open_to_talk),
            is_talking_to_you=(row.is_talking_to_you if talking is None
                               else talking),
            can_speak=row.can_speak if can_speak is None else can_speak,
            is_on_same_map=row.is_on_same_map if same_map is None else same_map,
            dist=row.dist if dist is None else dist,
            last_seen_ms=max(row.last_seen_ms, _coerce_ms(obs.observed_at_ms)),
            thread_id=(row.thread_id if obs.thread_id is None
                       else str(obs.thread_id)),
        )
