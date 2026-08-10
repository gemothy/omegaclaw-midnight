# Derived image: OmegaClaw + Midnight City direct-control plugin.
#
# Built FROM the published image rather than from source because the upstream
# build context is not distributed. Only the files this fork changes are
# copied in; ENTRYPOINT/CMD are inherited unchanged.
#
# Upstream: OmegaClaw v0.1.18, Apache-2.0 (relicensed from MIT 2026-07-22).
# NOTICE is preserved from the base image.
FROM singularitynet/omegaclaw:latest

ARG CORE=/PeTTa/repos/OmegaClaw-Core

# psycopg v3 backs the grounded-memory roster store. Installed from apt, not pip:
# the base image ships no pip module at all (`python3 -m pip` -> not found), and
# Debian 12 carries python3-psycopg 3.1.7. The store degrades to an in-process
# backend if this is ever missing, so the image still boots without it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-psycopg \
 && rm -rf /var/lib/apt/lists/*

# New plugin
COPY --chown=root:root plugins/mcity/mcity.metta       ${CORE}/plugins/mcity/mcity.metta
COPY --chown=root:root plugins/mcity/mcity_client.py   ${CORE}/plugins/mcity/mcity_client.py
COPY --chown=root:root plugins/mcity/README.md         ${CORE}/plugins/mcity/README.md

# Grounded memory: relevance-ranked projection + roster store. These are imported
# ABSOLUTELY by mcity_client (the plugin dir is appended to sys.path by
# src/plugin.py, so mcity_client is a top-level module, not a package). Names are
# prefixed mcity_* because that dir is appended LAST and a generic name like
# `store` would be silently shadowed by any installed package of the same name.
COPY --chown=root:root plugins/mcity/mcity_projection.py ${CORE}/plugins/mcity/mcity_projection.py
COPY --chown=root:root plugins/mcity/mcity_store/        ${CORE}/plugins/mcity/mcity_store/

# Registration, gateway routes, config plumbing and command parsing
COPY --chown=root:root config/plugins.yaml             ${CORE}/config/plugins.yaml
COPY --chown=root:root config/config.yaml              ${CORE}/config/config.yaml
COPY --chown=root:root proxy/nginx.conf.template       ${CORE}/proxy/nginx.conf.template
# nginx actually renders from /opt/nginx (owned by www-data), NOT from the repo
# proxy/ dir. The base image copied the originals there at build time, so repo
# copies alone have no effect. All three are needed: nginx.sh renders the
# control-mode routes from its own template into /opt/nginx/mcity-control.conf,
# which nginx.conf.template unconditionally includes.
COPY --chown=www-data:www-data --chmod=600 proxy/nginx.conf.template               /opt/nginx/nginx.conf.template
COPY --chown=www-data:www-data --chmod=600 proxy/nginx.mcity-control.conf.template /opt/nginx/nginx.mcity-control.conf.template
COPY --chown=www-data:www-data --chmod=600 proxy/nginx.sh                          /opt/nginx/nginx.sh
COPY --chown=root:root src/helper.py                   ${CORE}/src/helper.py
COPY --chown=root:root channels/telegram.py                ${CORE}/channels/telegram.py
COPY --chown=root:root profile/policy.yaml             ${CORE}/profile/policy.yaml
COPY --chown=root:root overlay/prompt.txt              ${CORE}/memory/prompt.txt
COPY --chown=root:root entrypoint.sh                   ${CORE}/entrypoint.sh

# Build-time gate. The final import check exercises the new modules exactly the
# way the plugin loader will (plugin dir on sys.path, absolute imports), so a
# missing __init__.py or a stray relative import fails the build instead of
# surfacing inside the live agent loop.
RUN chmod 0755 ${CORE}/entrypoint.sh \
 && chmod 0644 ${CORE}/plugins/mcity/*.metta ${CORE}/plugins/mcity/*.py \
 && chmod 0644 ${CORE}/plugins/mcity/mcity_store/*.py \
 && python3 -c "import ast;ast.parse(open('${CORE}/plugins/mcity/mcity_client.py').read())" \
 && python3 -c "import yaml;yaml.safe_load(open('${CORE}/config/plugins.yaml'))" \
 && python3 -c "import psycopg;print('psycopg',psycopg.__version__)" \
 && python3 -c "import sys;sys.path.insert(0,'${CORE}/plugins/mcity');import mcity_projection, mcity_store;print('projection+store import OK')"
