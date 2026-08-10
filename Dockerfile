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

# New plugin
COPY --chown=root:root plugins/mcity/mcity.metta       ${CORE}/plugins/mcity/mcity.metta
COPY --chown=root:root plugins/mcity/mcity_client.py   ${CORE}/plugins/mcity/mcity_client.py
COPY --chown=root:root plugins/mcity/README.md         ${CORE}/plugins/mcity/README.md

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

RUN chmod 0755 ${CORE}/entrypoint.sh \
 && chmod 0644 ${CORE}/plugins/mcity/*.metta ${CORE}/plugins/mcity/*.py \
 && python3 -c "import ast;ast.parse(open('${CORE}/plugins/mcity/mcity_client.py').read())" \
 && python3 -c "import yaml;yaml.safe_load(open('${CORE}/config/plugins.yaml'))"
