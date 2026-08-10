# Grounded Memory Architecture

Contract for the memory/projection refactor. Every module below builds to these
interfaces; do not invent alternatives.

## Why

The agent hallucinated its own state (claimed `hunger: normal` while the world API
returned `"state":"starving"`, claimed `200 meme_coin` against an actual `9776`,
and misquoted its own last message three different ways).

Root cause is not the model. Given a clean result block the model reports all facts
correctly in 23 tokens. The cause is **positional truncation**: every layer discards
information by string position rather than relevance.

| Layer | Mechanism | Loss |
|---|---|---|
| `mcity_client._cap()` | `text[:2000]` | 98.8% of the 284-agent roster |
| `memory.metta getHistory` | `read_file_tail(30000)` | oldest-but-relevant history |
| `maxFeedback` | 50000 char cap | skill results |

Fix: replace positional truncation with **relevance-ranked projection under an
explicit budget**, backed by a queryable store.

## Interfaces

### 1. `RosterStore` — structured world state

Backend-agnostic. No SQLite in the shipped default: single-writer and no network
access make it unfit for a multi-agent framework. It remains available only as a
test double.

```python
class RosterStore(Protocol):
    def upsert_agents(self, observed: Sequence[AgentObservation]) -> None: ...
    def mark_spoken(self, agent_id: str, at_ms: int, text: str) -> None: ...
    def candidates(self, *, now_ms: int, cooldown_ms: int, limit: int
                   ) -> list[AgentRow]: ...
    def get(self, agent_id: str) -> AgentRow | None: ...
    def health(self) -> bool: ...
```

#### Observation schema

The world API returns these per agent (verified live against 284 records). The
current `agents()` renderer discards all but `id`/`name`/`distance`, which is a
second defect independent of truncation: the system prompt instructs the agent to
"speak to one whose status is idle", but `status` never reaches the model, and
`isTalkingToYou` — someone actively addressing it — is thrown away too.

| Field | Use |
|---|---|
| `id`, `name` | identity |
| `status` | prompt filters on this; must be preserved |
| `isOpenToTalk` | stronger signal than `status` for candidacy |
| `isTalkingToYou` | **highest priority** — someone is already addressing us |
| `canSpeak`, `isOnSameMap` | eligibility gates |
| `distance` | tie-break |
| `profession` | context for opening lines |

`candidates()` is the query that ends the greeting loop. It MUST rank by
`(spoke_count ASC, last_spoken_ms ASC, dist ASC)` filtered to
`status='idle' AND last_spoken_ms < now-cooldown`. Vector similarity cannot express
this predicate; do not attempt to.

### 2. `ContextSource` — everything that wants prompt space

```python
@dataclass(frozen=True)
class Candidate:
    text: str
    score: float          # 0..1, higher = more worth including
    source: str
    pinned: bool = False  # survives budget pressure

class ContextSource(Protocol):
    name: str
    def candidates(self, turn: TurnState) -> Iterable[Candidate]: ...
```

### 3. `Budgeter` — allocates the context window

```python
class Budgeter:
    def render(self, sources: Sequence[ContextSource], turn: TurnState,
               budget_chars: int) -> str: ...
```

Rules, in order:
1. All `pinned` candidates are emitted first and never dropped.
2. Remaining budget is split across sources by declared weight, then filled by
   descending score.
3. A source that under-uses its share releases the remainder to others.
4. Anything dropped is summarised as a count, never silently cut:
   `[roster: 8 of 284 shown, ranked by unspoken-then-nearest]`.

Rule 4 is non-negotiable. Silent truncation is the original defect.

## Backends

| Backend | Role | Notes |
|---|---|---|
| `PostgresStore` | shipped default | `pgvector/pgvector:pg16`, dedicated container, port 5433 |
| `QdrantStore` | scale-out semantic + payload filter | optional |
| `InMemoryStore` | tests only | never a deployment target |

Do NOT repurpose the existing `nexus-postgres` (port 5432) — it holds unrelated data
and lacks the `vector` extension.

## Semantic layer

Chroma stays for content recall via `petta_lib_chromadb`; it is unaffected by this
work. Vector search answers "who did I discuss X with"; the roster answers "who have
I not spoken to". These are different questions and different stores.

## Atomspace projection

Structured facts project into Atomspace per turn so NAL/PLN can reason over social
state with truth values, e.g.

```metta
(|- ((--> (× gem frikkie) spoke-with) (stv 1.0 0.9))
    ((--> frikkie idle)               (stv 0.8 0.7)))
```

Atomspace is a **reasoning projection, not the system of record**. Durability and
ranking stay in the store.

## Degradation

The agent runs unattended in a loop. Memory infrastructure is an *enhancement* and
must never become a new way for it to die.

- Store unreachable at startup → log once, fall back to a bounded in-process store,
  keep serving. Never raise into a skill call.
- Store fails mid-turn → that turn renders without roster ranking, degrading to
  distance-ordered rows. Never propagate the exception to MeTTa.
- Every store call is wrapped with an explicit timeout. A hung database must not
  hang the agent loop; `mcity_client` already uses `DEFAULT_HTTP_TIMEOUT` for the
  world API and store calls must be equally bounded.
- Fallback state is reported, never silent: emit `store=degraded` in the skill line
  so a hallucination investigation can tell grounded turns from ungrounded ones.

## Non-goals

- Rewriting OmegaClaw-Core. This is an overlay fork; only files listed in the
  Dockerfile reach the image.
- Replacing Chroma.
- Embedding the roster. It is a filter/sort problem.
