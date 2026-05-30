# joplin-mcp: wraps erickt23/joplin-server-mcp (stdio) with supergateway (HTTP/SSE)
# Public default pulls python:slim straight from Docker Hub. In a CI environment
# with a registry pull-through cache (e.g. GitLab dependency proxy), set
# --build-arg REGISTRY=<cache-prefix>/ to route the base image through it.

ARG REGISTRY=
FROM ${REGISTRY}python:slim

# ARG changes daily (passed from CI as $(date +%Y%m%d)) so this RUN's
# cache key invalidates once per day, picking up newly-published security
# patches via `apt upgrade` against current debian repos.
ARG CACHEBUST_DAY=unset
RUN echo "cache day: ${CACHEBUST_DAY}" && \
    apt-get update && apt-get -y upgrade && \
    apt-get install -y --no-install-recommends \
    nodejs npm curl nginx gettext-base \
    && rm -rf /var/lib/apt/lists/*

# Install supergateway globally (v7+ required for streamableHttp transport)
RUN npm install -g supergateway

WORKDIR /app

# Vendored fork of erickt23/joplin-server-mcp (upstream pinned at
# d463635437fcda55b212706a9e81233f237e1b25). Carries:
#   - UTF-8 response-encoding fix for joppy (was patch_mcp.py:UTF8_FIX)
#   - model_validator on every *Input model to accept JSON-string args
#     (was patch_mcp.py:VALIDATOR)
#   - patch primitives: joplin_append_to_section, joplin_replace_section,
#     joplin_apply_patch — surgical edits that never serialize the full body
COPY joplin_server_mcp.py /app/joplin_server_mcp.py

# Install Python dependencies
RUN pip install --no-cache-dir \
    "mcp>=1.0.0" \
    "joppy>=1.0.0" \
    "pydantic>=2.0.0" \
    "httpx>=0.24.0"

COPY nginx.conf.template /etc/nginx/nginx.conf.template
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 8080

# nginx (8080) validates MCP_BEARER_TOKEN from Authorization header or ?token= query param,
# then proxies to supergateway (8081, internal) using streamableHttp transport.
ENTRYPOINT ["/app/start.sh"]
