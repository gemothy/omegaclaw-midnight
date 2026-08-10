"""Contract types for the Midnight City roster store.

`docs/ARCHITECTURE-memory.md` is the binding contract for this package; the
`RosterStore` protocol below transcribes its interface verbatim. The roster
answers "who have I not spoken to" — a filter/sort problem the architecture
explicitly keeps OUT of the vector layer, so nothing in this package embeds
anything.

Conventions every backend must share, or the backends stop being
interchangeable and the tests stop meaning anything:

  * `last_spoken_ms == 0` means "never spoken to". Zero sorts first under
    `last_spoken_ms ASC` and passes the cooldown filter for any realistic
    wall clock, which is exactly what a fresh introduction target needs, and
    it keeps the cooldown predicate a single comparison in both backends.
  * `dist is None` means "distance unknown". Unknown distances rank AFTER
    every known distance inside the same rank group, matching Postgres
    `ASC NULLS LAST`.
  * A field that is `None` on an `AgentObservation` means "this observation
    says nothing about it": merges never clobber a known value with an
    unknown one, so an agents-list row and a context row reinforce instead
    of erasing each other.
  * The eligibility flags default to False on a row that was never
    positively observed (for example one created by `mark_spoken` alone):
    an agent only ever becomes a greeting candidate on the strength of a
    real observation.
  * Ranking is `is_talking_to_you DESC` first — someone already addressing
    us always wins, it is a hard key, not a tie-break — then the original
    `(spoke_count ASC, last_spoken_ms ASC, dist ASC)`, then `agent_id ASC`
    purely so full ties stay deterministic across backends.

Only the Python standard library is used.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


# --------------------------------------------------------------------------
# shared conventions
# --------------------------------------------------------------------------

DEFAULT_NAME = ""
DEFAULT_STATUS = "unknown"
DEFAULT_PROFESSION = ""
NEVER_SPOKEN_MS = 0

_TRUE_WORDS = frozenset(("true", "1", "yes", "y", "on"))
_FALSE_WORDS = frozenset(("false", "0", "no", "n", "off", ""))


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AgentObservation:
    """One sighting of one agent, as scraped from the world by the caller.

    Field names follow docs/ARCHITECTURE-memory.md "Observation schema"
    (wire names `isOpenToTalk`, `isTalkingToYou`, `canSpeak`, `isOnSameMap`,
    `distance` arrive here in snake_case). `agent_id` is the only required
    field; every optional field left as None is treated as "not stated" and
    keeps whatever the store already knows.

    `thread_id` is not part of the world's agents payload: the caller fills
    it in when a threads listing or a delivered-speak confirmation reveals
    which thread belongs to this agent.
    """

    agent_id: str
    name: str | None = None
    status: str | None = None
    is_open_to_talk: bool | None = None
    is_talking_to_you: bool | None = None
    can_speak: bool | None = None
    is_on_same_map: bool | None = None
    dist: float | None = None
    profession: str | None = None
    observed_at_ms: int = 0
    thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRow:
    """The stored truth about one agent — filter/sort columns only, never an
    embedding. `last_seen_ms` merges monotonically (an old observation can
    never rewind it); `last_spoken_ms`/`spoke_count`/`last_spoken_text` are
    owned by `mark_spoken` and are untouched by observations."""

    agent_id: str
    name: str = DEFAULT_NAME
    status: str = DEFAULT_STATUS
    profession: str = DEFAULT_PROFESSION
    is_open_to_talk: bool = False
    is_talking_to_you: bool = False
    can_speak: bool = False
    is_on_same_map: bool = False
    dist: float | None = None
    last_seen_ms: int = 0
    last_spoken_ms: int = NEVER_SPOKEN_MS
    spoke_count: int = 0
    thread_id: str | None = None
    last_spoken_text: str = ""


# --------------------------------------------------------------------------
# the protocol (docs/ARCHITECTURE-memory.md, "Interfaces", section 1)
# --------------------------------------------------------------------------

@runtime_checkable
class RosterStore(Protocol):
    """Structured world state. Backend-agnostic; no SQLite in the shipped
    default (single-writer, no network access — unfit for a multi-agent
    framework). `PostgresStore` is the shipped backend, `InMemoryStore` is
    a test double only."""

    def upsert_agents(self, observed: Sequence[AgentObservation]) -> None:
        """Merge a batch of observations into the roster, one row per
        agent_id (INSERT ... ON CONFLICT DO UPDATE semantics: safe under
        concurrent writers). None fields keep the stored value; observations
        with an empty agent_id are skipped, never fatal."""

    def mark_spoken(self, agent_id: str, at_ms: int, text: str) -> None:
        """Record that we spoke to `agent_id` at `at_ms` saying `text`:
        increments spoke_count, sets last_spoken_ms and last_spoken_text.
        Creates the row if the agent was never observed (with every
        eligibility flag False, so it cannot become a candidate by being
        spoken to alone)."""

    def candidates(self, *, now_ms: int, cooldown_ms: int, limit: int
                   ) -> list[AgentRow]:
        """The query that ends the greeting loop. Filtered to
        `can_speak AND is_on_same_map AND (is_open_to_talk OR
        is_talking_to_you) AND last_spoken_ms < now_ms - cooldown_ms`
        (strict: a spoke exactly at the cooldown boundary is still inside
        it), ranked by `is_talking_to_you DESC` then `(spoke_count ASC,
        last_spoken_ms ASC, dist ASC)`, unknown dist last, agent_id as the
        final deterministic tie-break. `status` is stored for the prompt but
        deliberately NOT filtered on: `is_open_to_talk` is the API's own
        "will accept conversation" signal and supersedes it."""

    def get(self, agent_id: str) -> AgentRow | None:
        """The stored row for one agent, or None when it was never seen."""

    def health(self) -> bool:
        """True when the backend can serve queries right now. Never raises:
        this is the probe the degradation path decides on."""


# --------------------------------------------------------------------------
# shared coercion and ranking helpers (imported by every backend so the
# two implementations cannot drift apart)
# --------------------------------------------------------------------------

def _coerce_ms(value):
    """Timestamp-ish value -> non-negative int milliseconds, 0 when unusable."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _coerce_dist(value):
    """Distance-ish value -> non-negative float, None when unknown/unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(0.0, number)


def _coerce_flag(value):
    """Flag-ish value -> bool, or None when the observation did not state it."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return None


def _rank_key(row):
    """The mandated candidate order as one sort key. Postgres mirrors this
    exactly with `ORDER BY is_talking_to_you DESC, spoke_count ASC,
    last_spoken_ms ASC, dist ASC NULLS LAST, agent_id ASC`; change either
    side only together with the other."""
    return (not row.is_talking_to_you,
            row.spoke_count,
            row.last_spoken_ms,
            float("inf") if row.dist is None else row.dist,
            row.agent_id)


def _eligible(row, cutoff_ms):
    """The mandated candidate filter. Postgres mirrors this exactly in the
    WHERE clause of its candidates query; change either side only together
    with the other."""
    return (row.can_speak
            and row.is_on_same_map
            and (row.is_open_to_talk or row.is_talking_to_you)
            and row.last_spoken_ms < cutoff_ms)
