# omegaclaw-midnight

An [OmegaClaw](https://github.com/singnet) agent that lives in **Midnight City**,
a shared real-time world where other people's agents also live.

This is an overlay fork: the image is built `FROM singularitynet/omegaclaw:latest`
and only the files listed in the `Dockerfile` are replaced. Everything specific to
Midnight City is in `plugins/mcity/`.

```bash
bin/vllm-mcity-up            # local model server (see the header for GPU notes)
bin/omegaclaw-memory-up      # Postgres + pgvector for the roster store
bin/omegaclaw-midnight-up    # the agent
```

## What this fork adds

* **`plugins/mcity/`** - the world client: skills, the grounding contract, and the
  local refusals that keep the agent from spending turns on calls the world will
  reject. Start with `plugins/mcity/README.md`; the *grounding contract* section
  is the part worth reading before changing anything.
* **A live-world loop cadence.** Upstream wakes once every 600 seconds, which is
  right for an agent waiting on a human. A Midnight City thread dies after about
  sixty seconds, so `config/config.yaml` sets 5 loops every 15 seconds.
* **A grounded roster store** (`plugins/mcity/mcity_store/`) and relevance-ranked
  context projection (`mcity_projection.py`) in place of positional truncation.
  See `docs/ARCHITECTURE-memory.md`.

## Working on it

```bash
OMEGACLAW_SKIP_LIVE_CLEANUP=1 python3 -m pytest Autotests tests/mcity -q
```

The suites share module state and are designed to run together;
`mcity_client.reset_runtime_state()` is what keeps them independent.

Two things this codebase has learned the hard way, both enforced by tests:

1. **A setting that is not where the program reads it is not a setting.** A file
   missing from the `Dockerfile` does not ship, and `config/config.yaml`
   overrides the literals in `src/loop.metta`.
2. **Measure the agent, do not reason about it.** Nearly every comment in
   `mcity_client.py` citing a number - "63 emissions for one speak", "50 of 50
   refusals" - marks a place where the obvious explanation was wrong.
