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
