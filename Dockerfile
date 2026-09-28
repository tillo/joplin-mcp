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

WORKDIR /app

# Node CLI tooling (supergateway HTTP/SSE bridge + joplin CLI, baked for the
# owner-share-sync CronJob) installed as a LOCAL project instead of
# `npm install -g`. The local install honours the `overrides` map in
# package.json, which forces CVE-fixed versions of joplin's vulnerable
# transitive deps (tar, sharp, terminal-kit, js-yaml, form-data, nanoid, …) —
# `npm install -g` ignores `overrides`, which is why the grype scan kept
# reporting "CVE HIGH/CRITICAL con correzione" in joplin's tree.
#
# The C toolchain is present only for this layer so sqlite3 compiles
# deterministically instead of depending on a prebuilt download; it's purged
# afterwards to stay lean. `npm` is purged too: after `apt-get purge -y npm &&
# apt-get autoremove`, the whole /usr/share/nodejs library tree (handlebars,
# pacote, picomatch, http-cache-semantics, …) is cascade-removed because the
# `nodejs` package that stays depends only on libnode115 + node-corepack — that
# removes the second, unrelated source of npm CVEs in the scan.
COPY package.json /app/package.json
RUN apt-get update && apt-get install -y --no-install-recommends make g++ \
    && npm install \
    && ln -s /app/node_modules/.bin/joplin /usr/local/bin/joplin \
    && ln -s /app/node_modules/.bin/supergateway /usr/local/bin/supergateway \
    && apt-get purge -y npm make g++ && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm

# Vendored fork of erickt23/joplin-server-mcp (upstream pinned at
# d463635437fcda55b212706a9e81233f237e1b25). Carries:
#   - UTF-8 response-encoding fix for joppy (was patch_mcp.py:UTF8_FIX)
#   - model_validator on every *Input model to accept JSON-string args
#     (was patch_mcp.py:VALIDATOR)
#   - patch primitives: joplin_append_to_section, joplin_replace_section,
#     joplin_apply_patch — surgical edits that never serialize the full body
COPY joplin_server_mcp.py /app/joplin_server_mcp.py

# Install Python dependencies
# mcp capped <2.0.0: upstream's 2.0.0 (released 2026-07-28) restructured the
# package and dropped mcp.server.fastmcp, which joplin_server_mcp.py imports
# directly. The floating >=1.0.0 bound let a scheduled CACHEBUST_DAY rebuild
# pick up 2.0.0 and ship a joplin-mcp image whose child process cannot start.
RUN pip install --no-cache-dir \
    "mcp>=1.0.0,<2.0.0" \
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
