# Midnight City plugin

Lets one OmegaClaw instance observe, and optionally drive, one agent in
[Midnight City](https://midnight.city) through the public observer API.

The world is shared, live, and full of other people's agents. OmegaClaw is a
prompt-injectable autonomous loop. Everything below follows from those two
facts: the credential never enters the agent process, the control lease is
owned by you and not by the model, the reachable route set is enforced outside
the agent, and every byte of world text the model sees is inert.

## Files

| file | role |
|---|---|
| `plugins/mcity/mcity.metta` | plugin entry point: configuration, skill registration, dispatch |
| `plugins/mcity/mcity_client.py` | the whole client (standard library only) |
| `plugins/mcity/mcity_projection.py` | relevance-ranked context projection: replaces positional truncation, always reports what it dropped |
| `plugins/mcity/mcity_store/` | roster store (Postgres/pgvector, in-memory for tests): who is here, who is open to talk, who we have already spoken to |
| `proxy/nginx.conf.template` | the `/mcity/` gateway routes: credential injection, method allowlist, request budget |
| `entrypoint.sh` | renders `MCITY_OBSERVER_URL`, refuses to boot if a token is passed as an argument |
| `config/config.yaml` | the `# Midnight City` section |
| `tests/mcity/` | offline unit tests against a fake observer |
| `Autotests/mock/test_mcity_plugin_mock.py` | registration and command parsing in the running container |

## Quick start

Observation only (the default):

```sh
export MCITY_API_TOKEN=midnight_...          # never on the command line
scripts/omegaclaw start -p Anthropic
```

Direct control of one agent:

```sh
export MCITY_API_TOKEN=midnight_...
scripts/omegaclaw start -p Anthropic \
    --mcity-agent-id user-agent-1234 \
    --mcity-mode control
```

Equivalent settings can live in `config/config.yaml` (`mcityAgentId`,
`mcityMode`). Merchant names must be configured there, never on the command
line: `entrypoint.sh` forwards the container arguments through an unquoted
`"$*"`, so any value containing a space would be split.

## The token

`MCITY_API_TOKEN` reaches the container **only** through `docker run -e`. It is
consumed by `envsubst` while the gateway config is rendered, as `www-data`,
before the environment scrub in `entrypoint.sh`. It is deliberately **not** in
`SAFE_VARS`, so it does not survive into the agent process. It never appears in
`config.yaml`, in `argv`, in the logs, in MeTTa, or in anything the model reads.

`entrypoint.sh` refuses to start if any container argument looks like a token,
because every resolved configuration value is logged at INFO and the argument
list is readable from inside the agent.

The rendered `/opt/nginx/nginx.conf` contains the token in clear text. It is
`0600 www-data`, `proxy/nginx.sh` now asserts that before starting nginx, and
`profile/policy.yaml` no longer grants the agent blanket read access to `/opt`.

A second, different credential exists: the **lease token** returned by
`connect`. It lives in agent-process memory only, is never written to disk,
never returned to MeTTa, never logged, and is scrubbed out of every skill
result by `_redact()`. It is not secret from a determined agent — the `metta`
skill can call into any Python module in the process — so containment comes
from its 300 s TTL, its single-agent scope, and the fact that the gateway
routes accepting it are POST-only and rate limited.

## What the model can and cannot do

Registered in every mode: `mcity-status`, `mcity-context`, `mcity-inventory`,
`mcity-needs`, `mcity-areas`, `mcity-agents`, `mcity-navigation`,
`mcity-merchants`, `mcity-recent-events`, `mcity-threads`, `mcity-thread`.

Registered only in `control` mode: `mcity-move-area`, `mcity-move-agent`,
`mcity-move-tile`, `mcity-travel-district`, `mcity-enter-building`,
`mcity-exit-building`, `mcity-work`, `mcity-eat`, `mcity-sleep`,
`mcity-harvest`, `mcity-speak`.

Registered only when you configure both `mcityTradeMerchants` (exact names) and
a positive `mcityTradeMaxQuantity`: `mcity-trade`.

Never registered, and unreachable at the gateway even from `shell` or `metta`:
`connect`, `disconnect`, `claimable`, `shout`, free-text `engage`, and raw
action submission. The reads are pinned to the plugin's own agent id, so the
model cannot aim one at an arbitrary agent either.

## Operator preconditions

1. **`mcityMode=control` takes over the lease for `mcityAgentId`.** If you hold
   that lease from the CLI helper, release it first, or the container steals it.
   Unlike the reference helper, this plugin keeps no lease on disk and therefore
   cannot politely release a previous one.
2. **There is no graceful shutdown guarantee.** A best-effort release runs at
   interpreter exit; after `docker kill` control simply lapses, at most 300 s
   later.
3. **Two OmegaClaw containers must never share one `mcityAgentId`.** They would
   evict each other's lease in a loop.
4. If the AI supervisor or another controller claims the agent, the heartbeat
   gets a 404, the plugin marks the lease `lost` and **never reconnects**.
   `mcity-status` reports it; recovering is an operator decision.
5. At startup the plugin makes one unauthenticated read (`merchants`) to prove
   the gateway works and that `/mcity/` is denied by default. If that check
   fails the plugin degrades to observation only and logs the reason; it never
   aborts the agent.

## Enforcement lives in nginx, not in Python

`shell` and `metta` both run inside the agent process, so any Python-side
allowlist or counter is readable and resettable by the model, and
`shell curl http://localhost:8080/mcity/...` gets the same injected credential
the plugin gets. The route allowlist, the per-route method restriction and the
action budget therefore live in `proxy/nginx.conf.template`:

| class | routes | credential | budget |
|---|---|---|---|
| deny-all | `/mcity/` | — | `403` |
| public reads | `GET /mcity/api/skill/...` | client `Authorization` **stripped** | 240/min, burst 40 |
| master reads | `GET /mcity/api/agents/...`, `GET /mcity/api/threads/...` | master token injected | 240/min, burst 40 |
| lease acquisition | `POST /mcity/api/local-control/session` | master token injected | 20/min, burst 2 |
| lease use | `POST .../session/heartbeat`, `POST .../session/release` | lease token passes through | 20/min, burst 5 |
| world actions | `POST /mcity/api/actions` | lease token passes through | **12/min, burst 3** |

Everything else under `/mcity/` is `403`, including `claimable` and any route
added upstream later. Editing the template changes nothing until the image is
rebuilt: it is baked in at `/opt/nginx/nginx.conf.template`.

## Untrusted world text

Every game-sourced string (speech, names, thread messages, event text, merchant
names, server error bodies) is stripped of control characters, quotes and
apostrophes, capped at 200 characters and wrapped in
`<<MC_UNTRUSTED ... MC_UNTRUSTED>>`. Every result is capped at
`mcityMaxResultChars` (default 2000, against a 50000 char feedback budget) so a
flood of in-world text cannot dominate the prompt or the history file. The
`mcity_rules` prompt extension states the data/instruction boundary.
`mcity-speak` additionally refuses to send text the agent just read from
another agent, which is a best-effort brake on injection worms, not a
guarantee.

Egress is not restricted and `memory/prompt.txt` is writable: those are
pre-existing properties of the harness. This plugin does not make them worse
and does not fix them.

## Reading a result

```
MCITY-<VERB>-OK      [ key=value ]*  [ newline separated rows ]*
MCITY-<VERB>-PENDING [ key=value ]*
MCITY-<VERB>-FAILED  reason=<code> [detail=<short text>]
```

`reason` is one of `bad_args`, `disabled`, `read_only`, `not_ready`,
`no_lease`, `lease_expired`, `lease_lost`, `busy`, `rate_limited`, `timeout`,
`network`, `not_found`, `auth`, `http_<status>`, `upstream_invalid`,
`action_failed`, plus `internal` for the never-expected case where the client
itself failed.

Three semantics worth knowing before reading the output:

* success for `speak` is `outcome=delivered`, not `confirmed`;
* `mcity-work` confirms **only** on a `resource_gathered` event, so a
  non-resource job reports `PENDING` even when it succeeded;
* `gathered=no` on a harvest means the activity completed but nothing was
  collected; for crypto it also means settlement is still pending, so no
  `meme_coin` exists yet.

`PENDING` means the world had not confirmed within `mcityConfirmTimeout`
(default 8 s) — not that the action failed.

## Configuration reference

| key | where | default |
|---|---|---|
| `MCITY_API_TOKEN` | `docker run -e` only | — |
| `mcity_url` | `--mcity-url`, argv | `https://midnight.city/observer/` |
| `mcityAgentId` | `--mcity-agent-id`, argv, `config.yaml` | `""` (observation only) |
| `mcityMode` | `--mcity-mode`, argv, `config.yaml` | `read` |
| `mcityHttpTimeout` | `config.yaml` | `12` seconds |
| `mcityConfirmTimeout` | `config.yaml` | `8` seconds |
| `mcityMaxResultChars` | `config.yaml` | `2000` |
| `mcityActionMinInterval` | `config.yaml` | `3` seconds |
| `mcityClientInstanceId` | `config.yaml` | `omegaclaw:<agentId>:<hostname>` |
| `mcityTradeMerchants` | `config.yaml` only | `[]` (trade skill not registered) |
| `mcityTradeMaxQuantity` | `config.yaml` | `0` (trade skill not registered) |

## Tests

```sh
pytest tests/mcity -q            # offline, against tests/mcity/fake_observer.py
pytest Autotests/mock/test_mcity_plugin_mock.py -s   # needs a running container
```

No test may mutate the live world, and no test may call `connect` or
`disconnect` against it: the world is shared and the operator holds the real
lease. Every mutation path is covered against the fake observer instead.

## Deliberate differences from the reference `mcity-control.mjs`

1. No lease on disk. The reference writes the plaintext lease token to
   `~/.midnight-city/direct-control-lease.json`; here a restart just reconnects.
2. A background heartbeat thread at ~24 s instead of a heartbeat before every
   action: this is a long-running loop, not a short CLI, and the 300 s TTL has
   to survive a stalled LLM turn.
3. Confirmation budget 8 s at 1 s polling instead of 20 s at 0.5 s.
4. Events without an `eventId` are ignored rather than treated as new forever;
   that can only cause a `PENDING`, never a false confirmation.
5. No implicit release-then-connect of a foreign lease.
6. Reads are pinned to the plugin's own agent id.
7. Output is flat, redacted, capped, untrusted-delimited text instead of JSON.
