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

## Per-user mode (`joplin_user_mcp.py`)

The bearer mode above gives every caller the one Joplin account in `JOPLIN_SERVER_EMAIL`. The same image also carries a second entry point for a multi-user chat front end (e.g. Open WebUI with per-user OAuth MCP connections), where each person must reach **their own** Joplin account and nobody else's:

```bash
docker run -d -p 8080:8080 \
  -e KEYCLOAK_ISSUER=https://idp.example.com/realms/<realm> \
  -e EXPECTED_AUDIENCE=<client id> -e EXPECTED_AZP=<client id> \
  -e ALLOWED_GROUPS=<group> \
  -e JOPLIN_URL=http://joplin:22300 -e JOPLIN_PUBLIC_URL=https://your-joplin.example.com \
  -e RESOURCE_URL=http://<this service>:8080/mcp \
  -e STORE_KEY=<Fernet key> -e STORE_PATH=/data/store.sqlite3 -v joplin-user-mcp:/data \
  --entrypoint python <image> /app/joplin_user_mcp.py
```

- Every request carries the person's own OIDC access token. It is validated locally (RS256 via JWKS, issuer, audience, `azp`, `email_verified`, membership of `ALLOWED_GROUPS`; an empty list lets nobody in).
- Joplin Server validates no OIDC token, so the door into each account is Joplin's own **applications** flow. The first call returns a link to `/applications/<id>/confirm`; the person signs in to Joplin, presses *Authorise*, and the server collects an application credential that Joplin hands out once. The person sees it on their Joplin *Applications* page (as `vchat.mdapi.ch`, Web) and can delete it there at any time.
- Credentials are stored encrypted with `STORE_KEY` (Fernet), keyed by an HMAC of the token subject, with the email they were issued for. Every new Joplin session checks that the account's email equals the token's email; a mismatch (or an end-to-end-encrypted account) is refused and the credential is removed in Joplin at once.
- Each person has their own HTTP session (joppy's own client shares one module-level session between instances, so it is not used for requests).
- The tools are those of `joplin_server_mcp.py`, unchanged, minus `joplin_delete_note`, `joplin_delete_folder` and `joplin_ping`, plus `joplin_connect` (status / link) and `joplin_disconnect` (forget the credential and remove it in Joplin).
- Logs carry the tool, a hash of the subject, the outcome and the duration; never an email address or note content.

Tests: `python -m pytest tests/` (no network needed).

## Build

```bash
docker build -t joplin-mcp .
```

When building in a CI that has a pull-through cache for Docker Hub (e.g. the GitLab dependency proxy), pass `--build-arg REGISTRY=<cache-prefix>/` to route `python:slim` through it.
