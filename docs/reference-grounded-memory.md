# Grounded memory: operator reference

How to deploy, verify and roll back the roster store and context projection.
Design rationale lives in [ARCHITECTURE-memory.md](ARCHITECTURE-memory.md).

## What it fixes

The agent reported its own state wrongly to its operator — `hunger: normal` while
the world API returned `starving`, `200 meme_coin` against an actual `9776`, and
three different versions of its own last message.

The model was not at fault. Given a clean result block it reports every fact
correctly in 23 tokens. The harness was discarding the facts before the model saw
them, by **position** rather than relevance:

- `mcity-agents` returned 171,569 bytes and was cut to 2,000 — the agent saw about
  29 of 284 agents, always the same ones.
- `status`, `isOpenToTalk` and `isTalkingToYou` were never rendered at all, so an
  instruction to "speak to one whose status is idle" could not be followed and the
  agent could not tell when somebody was already talking to it.
- Starved of fresh data, status answers were reconstructed from history, which
  contained the agent's own earlier guesses. The numbers drifted with each retelling.

Measured on the unfixed harness over 40 minutes: `mcity-threads` 316,
`mcity-agents` 212, `mcity-work` 28, `mcity-eat` 4, `mcity-trade` **0** — 93% of
skill calls were redundant reads, while the agent starved holding 10,764 meme_coin.

## Configuration

| key | default | meaning |
|---|---|---|
| `mcityMemoryBackend` | `postgres` | `postgres` or `memory`. `memory` is for tests. |
| `mcityMaxResultChars` | `2000` | Projection budget per skill result. |
| `OMEGACLAW_PG_HOST` | `127.0.0.1` | |
| `OMEGACLAW_PG_PORT` | `5433` | Deliberately not 5432; `nexus-postgres` is unrelated. |
| `OMEGACLAW_PG_DB` / `_USER` | `omegaclaw` | |
| `OMEGACLAW_PG_PASSWORD` | — | From `~/.config/omegaclaw/memory.env` (0600) or `.env`. |

## Deploy

```bash
omegaclaw-memory-up                 # pgvector on 127.0.0.1:5433
cd ~/omegaclaw-midnight
docker build -t omegaclaw-midnight:latest .
omegaclaw-midnight-up               # restarts the agent on the new image
```

Or `docker compose up -d` after `cp .env.example .env` (see the compose file).

The build gate import-checks `mcity_projection` and `mcity_store` the way the
plugin loader does, so a bad import fails the build rather than the agent loop.

## Verify

Run all three. The first two are the actual acceptance criteria.

```bash
# 1. Does the agent's own reporting match the world?  exit 0 clean / 1 contradictions / 2 unavailable
python3 scripts/eval_grounding.py --since 30m

# 2. Is the loop acting, or just re-reading?  Read ratio should fall well below the 85-93% baseline.
python3 scripts/measure_loop_health.py --since 30m

# 3. Hermetic suite - no live agent, no database needed.
OMEGACLAW_SKIP_LIVE_CLEANUP=1 python3 -m pytest Autotests/test_mcity_*.py tests/ -q
```

Eyeball one rendered roster too: rows must carry `status`, and a truncated list
must end in an explicit `[roster: N of M shown ...]` footer. A silent cut is the
original defect and means the projection layer is not in the path.

## Degradation

Memory is an enhancement and must never become a new way for the agent to die.
If Postgres is unreachable the store falls back to an in-process backend, the
agent keeps running, and affected skill lines carry `store=degraded`. When
triaging a suspected hallucination, check for that marker first: it distinguishes
a grounded turn from an ungrounded one.

## Never take the control lease with a different clientInstanceId

The world allows **one control session per agent**. Acquiring a lease with a
`clientInstanceId` other than the running agent's displaces the agent's own
session, and every agent-scoped skill route then returns 404:

    GET /mcity/api/skill/agents/<id>/needs      -> 404
    GET /mcity/api/skill/merchants              -> 200   (global routes still fine)
    ... and the agent's own skills fail with MCITY-NEEDS-FAILED reason=not_ready

This happened during a manual intervention that used `clientInstanceId:
nanny:<agent>`; the agent was blind for roughly fifteen minutes. The global
routes staying healthy makes it look like a world outage rather than a stolen
session, which is what makes it worth writing down.

If you must drive the agent by hand, use the SAME id the agent uses
(`omegaclaw:<agent_id>`), and restart the container afterwards so the agent
reclaims and heartbeats its own lease. Recovery is just:

```bash
omegaclaw-midnight-up      # agent re-acquires its session
```

Note also that `mcity-move-area` only walks **within the current district**.
Crossing districts needs the `travel_to_district` action; a cross-district
`move_to` is accepted with an actionId and then silently never executes.

## Roll back

The overlay is a normal git history; every step is a separate commit.

```bash
git -C ~/omegaclaw-midnight log --oneline
git -C ~/omegaclaw-midnight revert <commit>     # or check out the baseline tree
docker build -t omegaclaw-midnight:latest . && omegaclaw-midnight-up
```

Rolling back needs no database change: the roster is derived state, rebuilt from
observation on the next few turns. Dropping `omegaclaw-pgdata` is safe and costs
only the accumulated spoke history.

## Caveat: this is an overlay fork

`Dockerfile` builds `FROM singularitynet/omegaclaw:latest` because the upstream
build context is not distributed. Only the files it COPYs are yours; everything
else in this tree is a reference copy of the published image (verified identical
at v0.1.18). **Any core file you change must be added to the COPY list or it
silently does not ship.** Contributing these modules upstream would remove that
whole class of mistake.
