# joplin-mcp

HTTP/SSE wrapper around [`erickt23/joplin-server-mcp`](https://github.com/erickt23/joplin-server-mcp) so a Joplin Server MCP can be reached over the network by Claude Code, Cursor, etc. — instead of only locally over stdio.

The upstream project ships a stdio MCP server. This image puts it behind:
- `supergateway` to expose it as **streamable HTTP / SSE**, and
- `nginx` for **bearer-token auth** (header or `?token=` query param), so the endpoint can be safely published behind your reverse proxy.

It also carries two small patches:

1. A **pydantic `model_validator`** injected into every `*Input` model that accepts a JSON-encoded string in addition to a parsed dict — FastMCP wraps large tool bodies as strings under supergateway's stdio framing, which the upstream models reject otherwise.
2. A **UTF-8 response-hook patch** to `joppy` (the Joplin client). Joplin Server's item `/content` endpoint returns `text/plain` with no charset, so `requests` mis-decodes the body and silently corrupts non-ASCII note content (mojibake compounds on every get→update). The patch pins `response.encoding = "utf-8"` for every joppy response. Upstream fix proposed at [marph91/joppy#36](https://github.com/marph91/joppy/pull/36); once merged the local patch can be dropped.

## Run

```bash
docker run -d \
  -p 8080:8080 \
  -e JOPLIN_SERVER_URL=https://your-joplin.example.com \
  -e JOPLIN_SERVER_EMAIL=you@example.com \
  -e JOPLIN_SERVER_PASSWORD=… \
  -e MCP_BEARER_TOKEN=<a long random secret> \
  ghcr.io/<your-org>/joplin-mcp:latest
```

Then point your MCP client at `https://<host>:8080/`, sending `Authorization: Bearer <MCP_BEARER_TOKEN>`.

## Build

```bash
docker build -t joplin-mcp .
```

When building in a CI that has a pull-through cache for Docker Hub (e.g. the GitLab dependency proxy), pass `--build-arg REGISTRY=<cache-prefix>/` to route `python:slim` through it.
