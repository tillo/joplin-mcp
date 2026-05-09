# joplin-mcp: wraps erickt23/joplin-server-mcp (stdio) with supergateway (HTTP/SSE)
# Built and pushed to registry.mdapi.ch/mdapi/joplin-mcp by GitLab CI.

FROM gitlab.mdapi.ch/mdapi/dependency_proxy/containers/python:slim

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

# Pin the server script at a known commit to avoid surprise breakage
ARG JOPLIN_MCP_COMMIT=main
ADD https://raw.githubusercontent.com/erickt23/joplin-server-mcp/${JOPLIN_MCP_COMMIT}/joplin_server_mcp.py /app/joplin_server_mcp.py

# Patch: add model_validator to all *Input pydantic models so they accept
# a JSON string as well as a dict (FastMCP passes a string for large bodies)
COPY patch_mcp.py /app/patch_mcp.py
RUN python /app/patch_mcp.py

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
