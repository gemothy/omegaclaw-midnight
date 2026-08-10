#!/bin/sh
set -eu

render() {
    template=$1
    output=$2
    vars=$(grep -o '\${[A-Z_0-9]*}' "${template}" | sort -u | tr '\n' ' ')
    touch "${output}"
    chmod 0600 "${output}"
    envsubst "${vars}" < "${template}" > "${output}"
    # The rendered files contain every provider secret in clear text, including
    # MCITY_API_TOKEN. They must never be readable by the agent uid.
    [ "$(stat -c '%a %U' "${output}")" = "600 www-data" ] || {
        echo "nginx.sh: refusing to start, ${output} permissions are wrong" >&2
        exit 1
    }
}

MCITY_CONTROL_CONF=/opt/nginx/mcity-control.conf

# Midnight City: the lease and action routes exist only in control mode. They
# are what a prompt-injected agent would need to mint its own lease with the
# operator's master token, or to submit an action kind that is not a registered
# skill, and `shell` reaches the gateway exactly like the plugin does. In read
# mode the file below stays empty and `location /mcity/` answers 403.
if [ "${MCITY_CONTROL:-off}" = "on" ]; then
    render /opt/nginx/nginx.mcity-control.conf.template "${MCITY_CONTROL_CONF}"
    echo "nginx.sh: Midnight City control routes are ENABLED (mcityMode=control)" >&2
else
    touch "${MCITY_CONTROL_CONF}"
    chmod 0600 "${MCITY_CONTROL_CONF}"
    printf '# Midnight City control routes are not rendered in this mode.\n' \
        > "${MCITY_CONTROL_CONF}"
    echo "nginx.sh: Midnight City control routes are DISABLED (observation only)" >&2
fi

render /opt/nginx/nginx.conf.template /opt/nginx/nginx.conf

nginx -c /opt/nginx/nginx.conf
