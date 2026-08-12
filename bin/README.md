# Launchers

The three scripts that stand the stack up. They were living untracked in
`~/bin`, which meant every operational fix learned the hard way — the peer-auth
ident map, the socket-only Postgres, the vLLM thinking template — existed on one
machine and would not have shipped to anyone adopting this.

| script | brings up |
|---|---|
| `vllm-mcity-up` | the model server on :8000 |
| `omegaclaw-memory-up` | Postgres + pgvector, socket-only, peer auth, least-privilege role |
| `omegaclaw-midnight-up` | the agent itself |

Order matters: memory and model first, agent last.

Secrets are never in these files. They are read from `~/.config/omegaclaw/*.env`
(mode 0600) and passed with `--env-file`, so nothing sensitive reaches argv or
shell history. `omegaclaw-memory-up` deliberately does not forward
`OMEGACLAW_PG_PASSWORD` to the agent at all: the roster authenticates over a
unix socket with peer auth, so the kernel vouches for the uid and there is no
credential in a prompt-injectable process to steal.

## Comparing a model before swapping one in

    python3 scripts/bench_model.py --capture --n 8      # the running server
    VLLM_MODEL=<candidate> bin/vllm-mcity-up            # swap
    python3 scripts/bench_model.py --n 8                # the candidate

It benches against the agent's REAL captured prompt, because that is the only
comparison that has ever decided anything here. Current baseline, on
nvidia/Qwen3.6-35B-A3B-NVFP4: median 0.77s per decision, 25.5 tok/s, and 4 of 6
replies opening with an mcity skill.

Revert is one command: VLLM_MODEL=nvidia/Qwen3-32B-FP4 bin/vllm-mcity-up
