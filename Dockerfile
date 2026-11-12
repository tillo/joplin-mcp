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
#
# .npmrc sets engine-strict: package.json `engines.node` states the node this
# base ships (Debian nodejs 20), and `npm install` fails on any package, the
# overrides included, whose engines range excludes it. Otherwise a pin like
# nanoid 6 (node >=22) installs with only a warning and works until a patch
# uses a 22-only API.
COPY package.json .npmrc /app/
RUN apt-get update && apt-get install -y --no-install-recommends make g++ \
    && npm install \
    && ln -s /app/node_modules/.bin/joplin /usr/local/bin/joplin \
    && ln -s /app/node_modules/.bin/supergateway /usr/local/bin/supergateway \
    && apt-get purge -y npm make g++ && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm

# The joplin CLI must actually work on this image's node before the image may
# be pushed: a note round trip through a filesystem sync target. It runs after
# the npm/toolchain purge, so it tests the runtime as shipped.
COPY smoke-joplin-cli.sh /app/smoke-joplin-cli.sh
RUN /app/smoke-joplin-cli.sh

# Vendored fork of erickt23/joplin-server-mcp (upstream pinned at
# d463635437fcda55b212706a9e81233f237e1b25). Carries:
#   - UTF-8 response-encoding fix for joppy (was patch_mcp.py:UTF8_FIX)
#   - model_validator on every *Input model to accept JSON-string args
#     (was patch_mcp.py:VALIDATOR)
#   - patch primitives: joplin_append_to_section, joplin_replace_section,
#     joplin_apply_patch — surgical edits that never serialize the full body
COPY joplin_server_mcp.py /app/joplin_server_mcp.py

# Per-user entry point (ns joplin-mcp-user): the same tools, each person on
# their own Joplin account through a Joplin "applications" credential, behind
# Keycloak tokens. Not used by start.sh; that Deployment runs
# `python /app/joplin_user_mcp.py` (not -I: it imports joplin_server_mcp from
# /app). See README.
COPY joplin_user_mcp.py /app/joplin_user_mcp.py

# Install Python dependencies
# mcp is pinned (==2.3.0): both entry points import MCPServer from
# mcp.server.mcpserver (mcp 2.x; FastMCP in 1.x) and joplin_user_mcp.py passes
# the transport settings to streamable_http_app(). A floating bound once let a
# scheduled CACHEBUST_DAY rebuild pick up 2.0.0 and ship a joplin-mcp image
# whose child process could not start, so a major bump is a port, not a pin change.
# PyJWT, cryptography, uvicorn and requests are for joplin_user_mcp.py.
# requirements.txt is the one list: CI's `test` job installs the same file.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY nginx.conf.template /etc/nginx/nginx.conf.template
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# MCP smoke (fails the build): bridge the server exactly as start.sh does and
# speak to it as a 2025-11-25 client — initialize, then tools/list must return
# tools. The overrides above float supergateway's own MCP SDK packages and lift
# its zod to 4 (infra-ik4p.26); a version that breaks the handshake builds and
# starts fine yet serves nothing. No Joplin credentials are needed: the server
# only logs in on the first tool call.
COPY smoke.mjs /app/smoke.mjs
RUN supergateway --stdio "python /app/joplin_server_mcp.py" \
      --outputTransport streamableHttp --port 9099 >/tmp/smoke-gateway.log 2>&1 & gw=$!; \
    node /app/smoke.mjs 9099; rc=$?; kill "$gw"; \
    [ "$rc" = 0 ] || tail -20 /tmp/smoke-gateway.log; rm -f /tmp/smoke-gateway.log; exit "$rc"

# Auth smoke (fails the build): only an Authorization: Bearer header gets past
# nginx; ?token= and no/wrong credential get 401. See smoke-auth.sh.
COPY smoke-auth.sh /app/smoke-auth.sh
RUN /app/smoke-auth.sh

EXPOSE 8080

# nginx (8080) validates MCP_BEARER_TOKEN from the Authorization header only,
# then proxies to supergateway (8081, internal) using streamableHttp transport.
ENTRYPOINT ["/app/start.sh"]
