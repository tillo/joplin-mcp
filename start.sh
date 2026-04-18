#!/bin/sh
set -e

if [ -z "$MCP_BEARER_TOKEN" ]; then
    echo "ERROR: MCP_BEARER_TOKEN is not set" >&2
    exit 1
fi

envsubst '${MCP_BEARER_TOKEN}' < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

supergateway --port 8081 --outputTransport streamableHttp \
    --stdio "python /app/joplin_server_mcp.py" &

exec nginx -g "daemon off;"
