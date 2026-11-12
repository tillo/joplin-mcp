#!/bin/sh
# Auth smoke for the nginx front, run by the Dockerfile on the image as built:
# render nginx.conf.template exactly as start.sh does (with a throwaway token),
# start nginx, and check who gets past the gate. Nothing listens on 8081 here,
# so a request the gate lets through answers 502, and a refused one 401.
#
# Why it exists: the credential is accepted ONLY as an Authorization: Bearer
# header. A ?token= query parameter puts it in URLs, which end up in client
# history and proxy/app logs (infra-az8.49), so it must stay refused.
set -eu

TOK=smoke-$$-token
T=$(mktemp -d)
trap 'nginx -c "$T/nginx.conf" -s stop 2>/dev/null || true; rm -rf "$T"' EXIT INT TERM
MCP_BEARER_TOKEN=$TOK envsubst "\${MCP_BEARER_TOKEN}" \
    < /etc/nginx/nginx.conf.template > "$T/nginx.conf"
nginx -c "$T/nginx.conf"
sleep 1

code() { curl -s -o /dev/null -w '%{http_code}' "$@" http://127.0.0.1:8080/mcp"${Q:-}"; }
fail=0
check() {  # name expected actual
    if [ "$3" = "$2" ]; then echo "ok   $1 -> $3"; else echo "FAIL $1 -> $3 (want $2)"; fail=1; fi
}
check "bearer header"       502 "$(code -H "Authorization: Bearer $TOK")"
check "wrong bearer"        401 "$(code -H "Authorization: Bearer nope")"
check "no credential"       401 "$(code)"
check "?token= query"       401 "$(Q="?token=$TOK" code)"
check "?token= plus header" 502 "$(Q="?token=$TOK" code -H "Authorization: Bearer $TOK")"
exit "$fail"
