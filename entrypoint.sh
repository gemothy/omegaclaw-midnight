#!/usr/bin/env bash
set -euo pipefail

# Adds slash at the end which is critical to Nginx configuration work properly
nginx_url() {
    text=$1
    [[ ${text} != */ ]] && text="${text}/"
    echo "${text}"
}

cd /PeTTa

EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-Local}"
OPENAIAPI_URL="http://localhost:8080/" # dummy value
MM_URL="http://localhost:8080/" # dummy value
MCITY_OBSERVER_URL="https://midnight.city/observer/" # Midnight City observer API
mcity_mode=""
mcity_agent_id=""
for arg in "$@"; do
  if [[ "$arg" == embeddingprovider=* ]]; then
    EMBEDDING_PROVIDER="${arg#*=}"
  fi
  # URL to redirect OpenAIAPI provider requests
  if [[ "$arg" == openaiapi_url=* ]]; then
    OPENAIAPI_URL=$(nginx_url "${arg#*=}")
  fi
  # URL to redirect Mattermost communication channel requests
  if [[ "$arg" == MM_URL=* ]]; then
    MM_URL=$(nginx_url "${arg#*=}")
  fi
  # URL of the Midnight City observer API proxied by the /mcity/ routes
  if [[ "$arg" == mcity_url=* ]]; then
    MCITY_OBSERVER_URL=$(nginx_url "${arg#*=}")
  fi
  # Which Midnight City routes the gateway publishes (see proxy/nginx.sh)
  if [[ "$arg" == mcityMode=* ]]; then
    mcity_mode="${arg#*=}"
  fi
  if [[ "$arg" == mcityAgentId=* ]]; then
    mcity_agent_id="${arg#*=}"
  fi
done

# The plugin resolves mcityMode/mcityAgentId from argv first and config.yaml
# second (src/config.py), so the gateway has to look in the same two places or
# a config.yaml-only operator would get a control plugin against a read gateway.
yaml_value() {
  local key="$1"
  local file="${OMEGACLAW_DIR:-/PeTTa/repos/OmegaClaw-Core}/config/config.yaml"
  [[ -f "${file}" ]] || return 0
  sed -n "s/^${key}:[[:space:]]*//p" "${file}" \
    | head -n1 \
    | sed -e 's/[[:space:]]*#.*$//' -e 's/["'"'"']//g' -e 's/[[:space:]]*$//'
}
[[ -n "${mcity_mode}" ]]     || mcity_mode="$(yaml_value mcityMode)"
[[ -n "${mcity_agent_id}" ]] || mcity_agent_id="$(yaml_value mcityAgentId)"

# Deny by default. The lease and action routes are only published when the
# operator explicitly asked for direct control AND supplied both an agent id and
# a token; in every other case they are 403 like any other unlisted route, so a
# prompt-injected `shell curl` cannot mint a lease with the master token.
MCITY_CONTROL="off"
if [[ "${mcity_mode}" == "control" && -n "${mcity_agent_id}" && -n "${MCITY_API_TOKEN:-}" ]]; then
  MCITY_CONTROL="on"
fi
echo "entrypoint: Midnight City gateway mode: ${MCITY_CONTROL} (mcityMode=${mcity_mode:-unset}, agent=${mcity_agent_id:+set}${mcity_agent_id:-unset}, token=${MCITY_API_TOKEN:+set})" >&2

export EMBEDDING_PROVIDER OPENAIAPI_URL MM_URL MCITY_OBSERVER_URL MCITY_CONTROL

# The agent's command line is forwarded verbatim below and every resolved
# configuration value is logged at INFO (src/config.py), so a token passed as an
# argument would end up in the agent's context and in the logs. The Midnight
# City token must reach nginx through the environment only.
for arg in "$@"; do
  case "$arg" in
    MCITY_API_TOKEN=*|mcity_api_token=*|mcityApiToken=*|*midnight_*)
      echo "entrypoint: refusing to start - the Midnight City API token must never be passed as an argument; use docker run -e MCITY_API_TOKEN=..." >&2
      exit 1
      ;;
  esac
done

su www-data -s /bin/sh -c "sh /opt/nginx/nginx.sh"

# Optional knowledge-base import
if [[ "${IMPORT_KB_ON_START}" == "1" ]]; then
  su nobody -s /bin/sh -c "${OMEGACLAW_DIR}/scripts/import_knowledge.sh"
fi

# Scrub environment: only allowlisted vars survive.
SAFE_VARS="HOME USER PATH HOSTNAME TERM LANG LC_ALL \
  PYTHONDONTWRITEBYTECODE PYTHONUNBUFFERED \
  HF_HOME SENTENCE_TRANSFORMERS_HOME HF_HUB_OFFLINE TRANSFORMERS_OFFLINE \
  OMEGACLAW_DIR MEMORY_DIR TEST_SERVER_IP \
  OMEGACLAW_PG_HOST OMEGACLAW_PG_PORT OMEGACLAW_PG_DB OMEGACLAW_PG_USER \
  OMEGACLAW_MEMORY_BACKEND"

# NOTE: OMEGACLAW_PG_PASSWORD is deliberately absent from SAFE_VARS. The roster
# store authenticates over a unix socket with peer auth, so the kernel vouches for
# the uid and no credential need ever enter this process. The agent has a shell
# skill and is prompt-injectable by a shared world; the only safe secret is one
# that does not exist here. Host/port/db/user are configuration, not credentials.
env_args=""
for var in $SAFE_VARS; do
  eval val=\${$var:-}
  if [ -n "$val" ]; then
    env_args="$env_args $var=$val"
  fi
done

exec env -i $env_args su nobody -s /bin/sh -c "sh run.sh run.metta GATEWAY_URL="http://localhost:8080" $*"
