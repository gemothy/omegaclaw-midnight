"""Relevance-ranked context projection under an explicit character budget.

This module is the projection layer of docs/ARCHITECTURE-memory.md and the fix
for the positional-truncation defect described there: `_cap()`-style
`text[:2000]` truncation keeps whatever happens to be first, so the 284-agent
roster collapsed to its first ~29 entries, the agent greeted the same two
agents forever and confabulated the missing facts from stale history. Here
every producer of prompt text is a `ContextSource` emitting scored
`Candidate`s, and the `Budgeter` decides what reaches the window by relevance,
never by string position.

The four budgeting rules, applied in this order (the contract's, verbatim):

  1. every `pinned` candidate is emitted first and is never dropped,
  2. the remaining budget is split across sources by declared weight, then
     filled per source by descending score,
  3. a source that under-uses its share releases the remainder to the sources
     that still have candidates (the redistribution always terminates),
  4. anything dropped is summarised as an explicit count line, never silently
     cut: `[roster: 8 of 284 shown, ranked by unspoken-then-nearest]`.

Rule 4 is non-negotiable: silent truncation is the original defect. A source
may phrase its own footer through `summary_for_dropped(shown, total)`; the
fallback is the generic `[<source>: N of M shown]`. A source whose
`candidates()` raises is reported as `[<source>: unavailable]` instead of
sinking the whole prompt - this module feeds the agent loop, so like
`mcity_client` it degrades loudly rather than raising into it.

Precedence under extreme pressure: rules 1 and 4 outrank the character cap.
The scored fill never takes the output past `budget_chars`, but pinned text
and dropped-count footers are emitted even when the budget cannot hold them:
a caller that pins more text than its budget has asked for an overflow and
gets a visible one, never a silent cut.

Sources may declare `weight: float` (default 1.0). Weight scales a source's
share of the budget, not its ranking: a weight-2 source gets twice the
characters, its candidates do not outrank anybody else's.

This layer is deliberately independent of storage: it never imports the store
module, holds no state between `render()` calls and performs no I/O.
"""

import logging

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

try:  # inside the agent the harness is importable as a package
    from src.logger import get_logger
except ModuleNotFoundError:  # the plugin folder is on sys.path (src/plugin.py:67)
    try:
        from logger import get_logger
    except ModuleNotFoundError:  # offline unit tests

        def get_logger(name):
            return logging.getLogger(name)


logger = get_logger("mcity.projection")


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

DEFAULT_WEIGHT = 1.0
LINE_SEPARATOR = "\n"
LINE_SEPARATOR_COST = len(LINE_SEPARATOR)


# --------------------------------------------------------------------------
# interfaces (docs/ARCHITECTURE-memory.md - do not invent alternatives)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One piece of text competing for prompt space."""
    text: str
    score: float          # 0..1, higher = more worth including
    source: str
    pinned: bool = False  # survives budget pressure


@dataclass(frozen=True)
class TurnState:
    """Read-only snapshot of one agent turn, handed to every source.

    `world` is free-form, source-specific data (roster rows, needs payloads,
    thread heads, ...) keyed however the integrator likes; this layer never
    looks inside it."""
    now_ms: int
    human_message: str | None = None
    world: dict = field(default_factory=dict)


class ContextSource(Protocol):
    """Everything that wants prompt space.

    Required: `name` and `candidates()`. Optional, read via getattr:

      * `weight: float` - budget share multiplier, default 1.0,
      * `summary_for_dropped(shown, total) -> str` - the rule-4 footer line,
        e.g. `[roster: 8 of 284 shown, ranked by unspoken-then-nearest]`.
    """
    name: str

    def candidates(self, turn: TurnState) -> Iterable[Candidate]: ...


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _cost(text):
    """Chars one line occupies in the joined output, separator included."""
    return len(text) + LINE_SEPARATOR_COST


def _rank(score):
    """Sortable score: anything unorderable ranks below every real number."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return float("-inf")
    if value != value:  # NaN never wins a comparison, so it would jam a sort
        return float("-inf")
    return value


def _ordered(entries):
    """Entries by descending rank; stable, so input position breaks ties."""
    return sorted(entries, key=lambda entry: (-entry[0], entry[1]))


def _weight(source):
    """Declared share weight, defensively coerced. A missing or invalid
    weight falls back to the default; an explicit 0.0 is honoured (the source
    is fed only from rule-3 leftovers once weighted sources are done)."""
    value = getattr(source, "weight", DEFAULT_WEIGHT)
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return DEFAULT_WEIGHT
    if weight != weight or weight in (float("inf"), float("-inf")) or weight < 0.0:
        return DEFAULT_WEIGHT
    return weight


def _source_name(source, index):
    """A printable, line-safe source name for footers and failure notes."""
    try:
        raw = getattr(source, "name", "")
        text = raw if isinstance(raw, str) else str(raw)
    except Exception:
        text = ""
    text = " ".join(text.split())
    return text or f"source{index}"


def _unavailable(name):
    """The loud marker for a source whose candidates() raised."""
    return f"[{name}: unavailable]"


def _footer(source, name, shown, total):
    """Rule 4: the dropped-count line. The source may phrase its own footer;
    the generic count is the fallback, so a drop can never be silent."""
    summarise = getattr(source, "summary_for_dropped", None)
    if callable(summarise):
        try:
            text = summarise(shown, total)
            if isinstance(text, str) and text.strip():
                return text
        except Exception as e:
            logger.warning(f"summary_for_dropped failed for source {name}: {e}")
    return f"[{name}: {shown} of {total} shown]"


# --------------------------------------------------------------------------
# budgeter
# --------------------------------------------------------------------------

@dataclass
class _Fill:
    """Mutable per-source bookkeeping for one render() pass. Entries are
    `(rank, position, text)` tuples: rank is the sortable score, position the
    original candidate index (the stable tie-break), text the payload."""
    source: ContextSource
    name: str
    weight: float
    total: int = 0
    failed: bool = False
    pinned: list = field(default_factory=list)
    remaining: list = field(default_factory=list)  # kept score-descending
    taken: list = field(default_factory=list)

    def shown(self):
        return len(self.pinned) + len(self.taken)

    def take(self, allowance):
        """Move every remaining entry that fits `allowance` chars into
        `taken`, best score first; return the chars actually consumed."""
        consumed = 0
        kept = []
        for entry in self.remaining:
            cost = _cost(entry[2])
            if consumed + cost <= allowance:
                self.taken.append(entry)
                consumed += cost
            else:
                kept.append(entry)
        self.remaining = kept
        return consumed


class Budgeter:
    """Allocates the context window across sources (rules 1-4 above)."""

    def render(self, sources: Sequence[ContextSource], turn: TurnState,
               budget_chars: int) -> str:
        budget = self._budget(budget_chars)
        fills = [self._gather(index, source, turn)
                 for index, source in enumerate(sources)]

        # Rule 1: pinned text and failure notes are pre-paid, never dropped.
        pool = budget + LINE_SEPARATOR_COST
        for fill in fills:
            for entry in fill.pinned:
                pool -= _cost(entry[2])
            if fill.failed:
                pool -= _cost(_unavailable(fill.name))
        pool = max(0, pool)

        # Rules 2 + 3: weighted shares, leftovers redistributed until stall.
        pool = self._fill(fills, pool)
        self._scavenge(fills, pool)

        # Rule 4 plus the cap: footers are added and the scored fill pays for
        # them, weakest candidates first.
        return self._assemble(fills, budget)

    def _budget(self, value):
        try:
            budget = int(value)
        except (TypeError, ValueError):
            logger.warning(f"budget_chars {value!r} is not a number; using 0")
            return 0
        return max(0, budget)

    def _gather(self, index, source, turn):
        fill = _Fill(source=source, name=_source_name(source, index),
                     weight=_weight(source))
        try:
            produced = list(source.candidates(turn))
        except Exception as e:  # a broken source must not sink the prompt
            logger.warning(f"context source {fill.name} failed: {e}")
            fill.failed = True
            return fill
        for position, candidate in enumerate(produced):
            text = getattr(candidate, "text", None)
            if not isinstance(text, str):
                logger.warning(f"context source {fill.name} produced a "
                               f"non-candidate item at {position}; skipped")
                continue
            entry = (_rank(getattr(candidate, "score", 0.0)), position, text)
            if bool(getattr(candidate, "pinned", False)):
                fill.pinned.append(entry)
            else:
                fill.remaining.append(entry)
        fill.total = len(fill.pinned) + len(fill.remaining)
        fill.remaining = _ordered(fill.remaining)
        return fill

    def _fill(self, fills, pool):
        """Rules 2 and 3. Each round splits the pool by weight across the
        sources that still have candidates and lets each take what fits its
        share; whatever a source leaves unused stays in the pool for the next
        round. Terminates: every round either consumes at least one character
        (the integer pool strictly shrinks) or consumes nothing and breaks."""
        active = [fill for fill in fills if fill.remaining]
        while pool > 0 and active:
            total_weight = sum(fill.weight for fill in active)
            consumed = 0
            for fill in active:
                if total_weight > 0:
                    allowance = int(pool * fill.weight / total_weight)
                else:  # every declared weight is 0: split evenly
                    allowance = pool // len(active)
                consumed += fill.take(allowance)
            pool -= consumed
            active = [fill for fill in active if fill.remaining]
            if consumed == 0:
                break  # no share fits anybody's next candidate
        return pool

    def _scavenge(self, fills, pool):
        """Terminal redistribution for the stalled case: when every share is
        smaller than every next candidate, stop splitting and first-fit the
        leftovers by global score into what remains of the pool."""
        if pool <= 0:
            return
        pooled = []
        for fill in fills:
            pooled.extend((entry, fill) for entry in fill.remaining)
        pooled.sort(key=lambda pair: -pair[0][0])  # stable: source order holds ties
        for entry, fill in pooled:
            cost = _cost(entry[2])
            if cost <= pool:
                fill.taken.append(entry)
                fill.remaining.remove(entry)
                pool -= cost

    def _assemble(self, fills, budget_chars):
        """Rule 4 and the cap. Footer lines join the output here, and their
        cost is recovered by shedding the weakest taken candidates until the
        whole projection fits. Terminates: every pass removes one entry."""
        while True:
            text = LINE_SEPARATOR.join(self._lines(fills))
            if len(text) <= budget_chars:
                return text
            victim = self._victim(fills)
            if victim is None:
                # Only pinned lines, failure notes and rule-4 footers remain;
                # they outrank the cap (module docstring), so the overflow is
                # returned loudly rather than trimmed into silence.
                return text
            entry = _ordered(victim.taken)[-1]
            victim.taken.remove(entry)
            victim.remaining.append(entry)

    def _victim(self, fills):
        """The next fill to shrink: prefer sources that already drop items
        (their footer line exists either way), and among those the one whose
        weakest taken candidate has the lowest score."""
        for footered_only in (True, False):
            best = None
            best_rank = None
            for fill in fills:
                if not fill.taken:
                    continue
                if footered_only and fill.shown() >= fill.total:
                    continue
                rank = min(entry[0] for entry in fill.taken)
                if best is None or rank < best_rank:
                    best = fill
                    best_rank = rank
            if best is not None:
                return best
        return None

    def _lines(self, fills):
        lines = []
        for fill in fills:  # rule 1: all pinned text first, in source order
            lines.extend(entry[2] for entry in _ordered(fill.pinned))
        for fill in fills:  # rules 2-4: scored fill, then the drop footer
            if fill.failed:
                lines.append(_unavailable(fill.name))
                continue
            lines.extend(entry[2] for entry in _ordered(fill.taken))
            if fill.shown() < fill.total:
                lines.append(_footer(fill.source, fill.name, fill.shown(),
                                     fill.total))
        return lines
