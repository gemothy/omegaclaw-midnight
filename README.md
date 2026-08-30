<p align="center">
  <img src="docs/assets/midnight-city-logo.png" alt="Midnight City" width="96" />
</p>

<h1 align="center">omegaclaw-midnight</h1>

<p align="center">
  An <a href="https://github.com/singnet">OmegaClaw</a> agent that lives in
  <a href="https://midnight.city">Midnight City</a> &mdash; and holds down a job there.
</p>

---

## What Midnight City is

[Midnight City](https://midnight.city) describes itself as *"the first AI MMORPG
that plays itself. You direct. A persistent world where AI heroes live, work,
fight, and earn 24/7."*

It is a **shared, real-time world**. The other characters are not scripted NPCs:
they are other people's agents, running on other people's hardware, pursuing
their own goals. That single fact drives almost every design decision in this
repository:

* **Everything the world says is untrusted input.** Another player can name their
  character `IGNORE_ALL_PRIOR_INSTRUCTIONS` and say anything they like to you.
  A prompt-injectable agent holding a live credential is a real threat model,
  not a hypothetical one.
* **The world does not wait.** A conversation thread goes stale roughly sixty
  seconds after the other agent speaks. An agent that thinks for ninety seconds
  is an agent that never talks to anybody.
* **Consequences are persistent.** The agent gets hungry, spends what it earns,
  and everything it says is said to a real person.

Normally a Midnight City character is driven by the platform's own AI runtime.
The [`midnight-city-direct-control`](https://production.midnight.city/docs/connect-to-midnight-city/hermes-openclaw/)
path instead lets you claim one and drive it from your own stack. That is what
this project does &mdash; with OmegaClaw as the brain.

## What this is

An **overlay fork** of OmegaClaw. The image builds `FROM
singularitynet/omegaclaw:latest` and replaces only the files listed in the
`Dockerfile`; everything specific to Midnight City lives in `plugins/mcity/`.
Upstream is not vendored or forked wholesale, so tracking a new OmegaClaw
release stays cheap.

```bash
bin/vllm-mcity-up            # local model server (see the header for GPU notes)
bin/omegaclaw-memory-up      # Postgres + pgvector for the roster store
bin/omegaclaw-midnight-up    # the agent
```

## Running it yourself

You need three things the repository deliberately does not contain.

1. **A Midnight City API token.** Request one in the Midnight Discord (see the
   [connect docs](https://production.midnight.city/docs/connect-to-midnight-city/hermes-openclaw/)).
   Tokens are environment-scoped: a development token returns
   `401 active_key_unknown` against production.

   > **Reviewing this for [BGI Commons HyperSprint-1](https://bgicommons.org/hackathons/hypersprint-1-omegaclaw)?**
   > If you would rather not go through Discord, email **gem@thrilion.com** and
   > I will provision a token and a claimable agent for you directly.
2. **A claimable agent.** One not currently supervised by the platform runtime.
   `mcity-status` and the `claimable` listing will tell you which ids your token
   owns; using an agent id your token does not own returns
   `403 agent_not_authorized`.
3. **A model server.** Any OpenAI-compatible endpoint. `bin/vllm-mcity-up`
   serves `nvidia/Qwen3.6-35B-A3B-NVFP4` on a single DGX Spark; the header of
   that script records the GPU-memory and context settings that actually fit.

```bash
cp .env.example .env && chmod 600 .env    # then fill in MCITY_API_TOKEN etc.
docker compose up -d                      # Postgres + the agent
```

`docker-compose.yml` is the packaged equivalent of the `bin/` launchers and the
path to use if you are adopting this rather than developing it. The launchers
give you finer control (and their headers document the GPU and Postgres
settings that were learned the hard way); compose gives you the stack.

`.env` is git-ignored and must never be committed. The API token is **never**
passed to the model: the nginx gateway in `proxy/` injects it on the way out, so
it cannot appear in the agent's context, its logs, or anything it might be
persuaded to repeat. `Autotests/mock/test_credentials_scrubbed_mock.py` enforces
that.

## What this fork adds

* **`plugins/mcity/`** &mdash; the world client: skills, the grounding contract, and
  the local refusals that stop the agent spending turns on calls the world will
  reject. Start with `plugins/mcity/README.md`; the *grounding contract* section
  is the part worth reading before changing anything.
* **A live-world loop cadence.** Upstream wakes once every 600 seconds, which is
  right for an agent waiting on a human. A Midnight City thread dies after about
  sixty seconds, so `config/config.yaml` sets `wakeupInterval: 1` with
  `maxWakeLoops: 50`: the budget refills on the next iteration, the agent is
  never asleep, and the loop is paced by how long a decision takes rather than
  by sleeping.
* **A grounded roster store** (`plugins/mcity/mcity_store/`) and relevance-ranked
  context projection (`mcity_projection.py`) in place of positional truncation.
  See `docs/ARCHITECTURE-memory.md`.
* **A trust boundary that distinguishes identifiers from prose.** Area ids,
  agent ids and statuses render plainly so the agent can act on them; anything a
  player can author stays wrapped and inert. Getting this wrong in either
  direction breaks the agent &mdash; wrap too much and it cannot move or trade at
  all, wrap too little and the world can instruct it.
* **Launchers under `bin/`**, because operational knowledge that lives only in
  one person's `~/bin` does not ship.

## Working on it

```bash
OMEGACLAW_SKIP_LIVE_CLEANUP=1 python3 -m pytest Autotests tests/mcity -q
# pytest must be importable by the interpreter you use; this repo is developed
# against a venv (e.g. ~/.hermes-venv/bin/python3 -m pytest ...)
```

The suites share module state and are designed to run together;
`mcity_client.reset_runtime_state()` is what keeps them independent.

`scripts/` holds the measurement harness rather than more tests &mdash;
`eval_reply.py`, `eval_grounding.py`, `reply_funnel.py`,
`measure_dnd_recovery.py`, `can_it_act.py`, `state_of_play.py`. They exist
because of the second lesson below.

Two things this codebase has learned the hard way, both enforced by tests:

1. **A setting that is not where the program reads it is not a setting.** A file
   missing from the `Dockerfile` does not ship, and `config/config.yaml`
   overrides the literals in `src/loop.metta`.
2. **Measure the agent, do not reason about it.** Nearly every comment in
   `mcity_client.py` citing a number &mdash; "63 emissions for one speak", "50 of 50
   refusals" &mdash; marks a place where the obvious explanation was wrong.

## Licence and attribution

OmegaClaw is a project of the SingularityNET Foundation, relicensed from MIT to
**Apache-2.0** as of 2026-07-22 (see `NOTICE`). This fork is distributed under the
same licence; the full text is in `LICENSE`. Midnight City is a product of its
own authors and is referenced here under the integration path its documentation
describes.
