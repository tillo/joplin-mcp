"""
joplin-user-mcp — the Joplin tools of joplin_server_mcp.py, for each person's own Joplin account.

One user, one token, one Joplin account. Every MCP request must carry the caller's own
Keycloak access token; this server then works with the Joplin "applications" credential that
this same person authorised for Chat, and with no other. It holds no Joplin password and no
shared or admin Joplin account.

    Open WebUI  --(user's Keycloak token, aud=openwebui-joplin-mcp)-->  this server
    this server --(that person's application credential, X-API-AUTH)-->  Joplin Server

Why an application credential and not the token
-----------------------------------------------
Joplin Server 3.x validates no bearer token, JWT or OIDC token: its only login is a session
(`POST api/sessions`). Its per-user, revocable door is the device-style "applications" flow:
the person opens `/applications/<uuid>/confirm` on notes.mdapi.ch, signs in, presses
Authorise, and the client fetches `{id, password}` from `api/application_auth/<uuid>` exactly
once. The person sees it, and can delete it, on notes.mdapi.ch/applications. Isolation between
people is then enforced by Joplin itself; this server only chooses which credential to use.

The credential is stored encrypted (Fernet, STORE_KEY) in SQLite, under an HMAC of the
Keycloak `sub`, together with the email it was issued for. Fail closed (D9): the Joplin
account behind the credential must have the same email as the token, on every new session.

Not offered here: deleting notes or notebooks (D3). The other tools are joplin_server_mcp's,
with their arguments flattened for small models (see HIDDEN), called with this person's own
Joplin session.

joppy note: joppy's ServerApi sends every request through ONE module-level requests.Session.
Two people in one process would share its cookie jar, so UserApi never calls
ServerApi.__init__ and sends through its own session.

Privacy: logs carry a hash of the subject, the tool, the outcome and the duration. Never an
email address, a note title, a body or an identifier from Joplin.

Environment:
    KEYCLOAK_ISSUER        e.g. https://idp.mdapi.ch/realms/tilloch
    EXPECTED_AUDIENCE      audience of incoming MCP tokens (openwebui-joplin-mcp)
    EXPECTED_AZP           client that must have minted them (openwebui-joplin-mcp); "" = any
    ALLOWED_GROUPS         `groups` claim values let in (ChatNotes); empty = nobody
    JOPLIN_URL             Joplin Server as this pod reaches it (in-cluster Service)
    JOPLIN_PUBLIC_URL      Joplin Server as the person's browser reaches it (notes.mdapi.ch)
    STORE_PATH             SQLite file on the PVC
    STORE_KEY              Fernet key (from a mounted Secret); required
    RESOURCE_URL           canonical URL of this MCP endpoint, for RFC 9728
    PORT                   listen port (default 8080)
"""

import asyncio
import datetime
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextvars import ContextVar
from typing import Annotated, Optional
from urllib.parse import urlencode, urlsplit

import jwt
import requests
from cryptography.fernet import Fernet
from joppy.server_api import LockError, ServerApi
from jwt import PyJWKClient
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError, WrapValidator
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

import joplin_server_mcp as jsm

log = logging.getLogger("joplin_user_mcp")

KEYCLOAK_ISSUER = os.environ.get("KEYCLOAK_ISSUER", "https://idp.mdapi.ch/realms/tilloch").rstrip("/")
EXPECTED_AUDIENCE = os.environ.get("EXPECTED_AUDIENCE", "openwebui-joplin-mcp")
EXPECTED_AZP = os.environ.get("EXPECTED_AZP", "openwebui-joplin-mcp")
JOPLIN_URL = os.environ.get("JOPLIN_URL", "http://joplin.joplin.svc.cluster.local:22300").rstrip("/")
JOPLIN_PUBLIC_URL = os.environ.get("JOPLIN_PUBLIC_URL", "https://notes.mdapi.ch").rstrip("/")
STORE_PATH = os.environ.get("STORE_PATH", "/data/joplin-user-mcp.sqlite3")
STORE_KEY = os.environ.get("STORE_KEY", "")
RESOURCE_URL = os.environ.get("RESOURCE_URL", "http://joplin-mcp-user.joplin-mcp-user.svc.cluster.local:8080/mcp")
PORT = int(os.environ.get("PORT", "8080"))

# Notes are as private as mail: a missing setting refuses everybody.
ALLOWED_GROUPS = [g.strip() for g in os.environ.get("ALLOWED_GROUPS", "").split(",") if g.strip()]

CERTS_URL = f"{KEYCLOAK_ISSUER}/protocol/openid-connect/certs"
METADATA_URL = f"{RESOURCE_URL.split('/mcp')[0]}/.well-known/oauth-protected-resource"

# What the person sees for this credential on notes.mdapi.ch/applications ("vchat.mdapi.ch",
# Web). applications.version is varchar(16). api/sessions overwrites these three on every
# login, so every login sends the same values.
CLIENT_INFO = {"version": "chat.mdapi.ch", "platform": "6", "type": "3"}  # platform Web, type Cli
HTTP_TIMEOUT = 20.0
# Joplin sessions expire 12 h after creation, whatever the activity.
SESSION_MAX_AGE = 10 * 3600.0
# A 403 on an older session triggers one fresh login. Bounded, because Joplin rate-limits
# logins per client IP and every person here shares this pod's IP.
RELOGIN_MIN_AGE = 60.0
PENDING_TTL = 30 * 60.0

jwks = PyJWKClient(CERTS_URL, cache_keys=True, lifespan=300)

_CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')


class AuthError(Exception):
    """Carries a short, PII-free reason; it is logged."""


class NoToken(Exception):
    pass


class UserFacing(Exception):
    """An outcome to hand back to the person, as a JSON tool result. `code` is logged."""

    def __init__(self, code: str, message: str, **extra):
        # str() is the message: joplin_server_mcp's tools return str(exception) to the caller.
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def payload(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


class Refused(UserFacing):
    """A credential that opens the wrong account or an unusable one: Chat drops it."""


class Revoked(Exception):
    """The application credential no longer logs in: the person deleted it in Joplin."""


# ── incoming token ───────────────────────────────────────────────────────────

def _validate_incoming(token: str) -> dict:
    try:
        key = jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=EXPECTED_AUDIENCE,
            issuer=KEYCLOAK_ISSUER,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except Exception as e:  # signature, expiry, issuer, audience
        raise AuthError(f"invalid token: {type(e).__name__}") from e

    if EXPECTED_AZP and claims.get("azp") != EXPECTED_AZP:
        raise AuthError("token was not minted for the Chat notes connection")
    if claims.get("email_verified") is not True or not claims.get("email"):
        raise AuthError("no verified email")
    if not ALLOWED_GROUPS:
        raise AuthError("server misconfigured: ALLOWED_GROUPS is empty")
    groups = claims.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if not set(ALLOWED_GROUPS) & {str(g) for g in groups}:
        raise AuthError("user is not in a group allowed to use notes in Chat")
    return claims


# ── per-request identity ─────────────────────────────────────────────────────
# (sub, email, who): who is the 12-hex hash that logs carry. Set by the auth middleware;
# FastMCP runs the tool call inside the request's task, and asyncio.to_thread copies it.
_current = ContextVar("joplin_user_mcp_identity", default=None)


def _identity() -> tuple[str, str, str]:
    cur = _current.get()
    if cur is None:
        raise NoToken("no token in request context")
    return cur


# ── credential store ─────────────────────────────────────────────────────────

class Store:
    """SQLite on the PVC. Rows are keyed by an HMAC of the Keycloak subject and hold only
    Fernet ciphertext, so the file alone names nobody and opens nothing."""

    def __init__(self, path: str, key: str):
        self._fernet = Fernet(key.encode())
        self._mac_key = hashlib.sha256(b"joplin-user-mcp row key\0" + key.encode()).digest()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        for table in ("creds", "pending"):
            self._db.execute(f"CREATE TABLE IF NOT EXISTS {table} (k TEXT PRIMARY KEY, blob BLOB NOT NULL, at REAL NOT NULL)")

    def _k(self, sub: str) -> str:
        return hmac.new(self._mac_key, sub.encode(), hashlib.sha256).hexdigest()

    def get(self, table: str, sub: str) -> tuple[dict, float] | None:
        with self._lock:
            row = self._db.execute(f"SELECT blob, at FROM {table} WHERE k = ?", (self._k(sub),)).fetchone()
        if row is None:
            return None
        return json.loads(self._fernet.decrypt(row[0])), row[1]

    def put(self, table: str, sub: str, value: dict) -> None:
        blob = self._fernet.encrypt(json.dumps(value).encode())
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO {table} (k, blob, at) VALUES (?, ?, ?)", (self._k(sub), blob, time.time()))

    def delete(self, table: str, sub: str) -> None:
        with self._lock:
            self._db.execute(f"DELETE FROM {table} WHERE k = ?", (self._k(sub),))


_store: Store | None = None


def _get_store() -> Store:
    global _store
    if _store is None:
        if not STORE_KEY:
            raise RuntimeError("STORE_KEY is not set")
        _store = Store(STORE_PATH, STORE_KEY)
    return _store


# ── Joplin, as one person ────────────────────────────────────────────────────

# Joplin Server answers 404 "Invalid origin" to a request whose Host is not APP_BASE_URL's.
# Reached through its in-cluster Service, the request still has to name the public host.
JOPLIN_HEADERS = ({"Host": urlsplit(JOPLIN_PUBLIC_URL).netloc}
                  if urlsplit(JOPLIN_URL).netloc != urlsplit(JOPLIN_PUBLIC_URL).netloc else {})


def _new_http() -> requests.Session:
    s = requests.Session()
    s.headers.update(JOPLIN_HEADERS)
    return s


class UserApi(ServerApi):
    """joppy's ServerApi bound to ONE application credential, with its own HTTP session.

    ServerApi.__init__ is deliberately not called: it logs in through joppy's module-level
    session, which every instance in the process shares."""

    def __init__(self, app_id: str, password: str, on_revoked=None):
        self.user, self.url = app_id, JOPLIN_URL
        self.client_id = uuid.uuid4().hex
        self.current_sync_lock = None
        self.lock_ttl = datetime.timedelta(seconds=60 * 3)
        self.lock_auto_refresh_interval = datetime.timedelta(seconds=60)
        self._password = password
        self._on_revoked = on_revoked
        self._http = _new_http()
        self.user_id = ""
        self.logged_in_at = 0.0
        self._login()
        if self.get_sync_version() is None:
            # An account no client has synced yet: the same info.json joppy writes.
            now = int(time.time() * 1000)
            self.put("/api/items/root:/info.json:/content", json={
                "version": 3,
                "e2ee": {"value": False, "updatedTime": now},
                "activeMasterKeyId": {"value": False, "updatedTime": now},
                "masterKeys": [],
                "ppk": {},
                "appMinVersion": "3.0.0",
            })

    def _login(self) -> None:
        r = self._http.post(f"{self.url}/api/sessions",
                            json={"email": self.user, "password": self._password, **CLIENT_INFO},
                            timeout=HTTP_TIMEOUT)
        if r.status_code in (401, 403):
            if self._on_revoked:
                self._on_revoked()
            raise Revoked("Joplin no longer accepts the credential Chat held: it was removed on "
                          f"{JOPLIN_PUBLIC_URL}/applications. Call joplin_connect to connect again.")
        if r.status_code == 429:
            raise UserFacing("joplin_busy", "Joplin is limiting logins right now; try again in a minute.")
        r.raise_for_status()
        j = r.json()
        self._http.headers["X-API-AUTH"] = j["id"]
        self.user_id = j["user_id"]
        self.logged_in_at = time.monotonic()

    def account_email(self) -> str:
        r = self._http.get(f"{self.url}/api/users/{self.user_id}", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return str(r.json().get("email") or "")

    def check_sync_target(self) -> None:
        """Refuse what joplin_server_mcp cannot work with: another sync version, or E2EE (it
        would read ciphertext, and write plain notes into an encrypted account)."""
        info = self.get("/api/items/root:/info.json:/content").json()
        if info.get("version") != 3:
            raise Refused("joplin_unsupported", "This Joplin account uses a sync format Chat does not support.")
        if (info.get("e2ee") or {}).get("value"):
            raise Refused("joplin_encrypted",
                             "This Joplin account uses end-to-end encryption, so Chat cannot read or "
                             "write its notes. Chat did not keep the connection.")

    def revoke_self(self) -> bool:
        """Delete this application in Joplin, as its owner, with the page's CSRF token."""
        r = self._http.get(f"{self.url}/applications", timeout=HTTP_TIMEOUT)
        m = _CSRF_RE.search(r.text) if r.ok else None
        if not m:
            return False
        r = self._http.post(f"{self.url}/applications/{self.user}/delete", data={"_csrf": m.group(1)},
                            allow_redirects=False, timeout=HTTP_TIMEOUT)
        return r.status_code in (302, 303)

    def _send(self, method, path, query, data, files, json, headers):
        query_str = "&".join(f"{k}={v}" for k, v in (query or {}).items())
        return getattr(self._http, method)(f"{self.url}{path}?{query_str}", data=data, files=files,
                                           json=json, headers=headers, timeout=HTTP_TIMEOUT)

    def _request(self, method, path, query=None, data=None, files=None, json=None, headers=None):
        # joppy's own _request (1.0.4), sent through self._http instead of the shared SESSION.
        if not (path == "/login" or path.startswith("/api/locks") or "info.json" in path
                or ".sync/version.txt" in path):
            if self.current_sync_lock is None:
                raise LockError("No sync lock. Acquire a lock before issuing requests.")
            if not self._is_lock_active(self.current_sync_lock.updatedTime):
                raise LockError("Sync lock expired. Delete the lock before issuing requests.")
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            if self.current_sync_lock.updatedTime + self.lock_auto_refresh_interval < now:
                self.current_sync_lock = self._add_lock()
        r = self._send(method, path, query, data, files, json, headers)
        if r.status_code == 403 and time.monotonic() - self.logged_in_at > RELOGIN_MIN_AGE:
            self._login()
            r = self._send(method, path, query, data, files, json, headers)
        # Joplin's item content is UTF-8 without a charset; see joplin_server_mcp.
        r.encoding = "utf-8"
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as err:
            err.args = err.args + (r.text,)
            raise
        return r


# Live sessions, per person, in memory only: sub -> (api, opened at, email it was checked for).
_apis: dict[str, tuple[UserApi, float, str]] = {}
_user_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _user_lock(sub: str) -> threading.Lock:
    # One call at a time per person: a UserApi holds one joppy sync lock at a time.
    with _registry_lock:
        return _user_locks.setdefault(sub, threading.Lock())


def _forget(sub: str) -> None:
    with _registry_lock:
        _apis.pop(sub, None)
    _get_store().delete("creds", sub)


def _confirm_url(aid: str) -> str:
    return f"{JOPLIN_PUBLIC_URL}/applications/{aid}/confirm?{urlencode(CLIENT_INFO)}"


def _not_connected(aid: str, email: str, waiting: bool) -> UserFacing:
    first = ("The link below is still waiting for you to press Authorise." if waiting
             else "Chat is not connected to your Joplin notes yet.")
    return UserFacing(
        "joplin_not_connected",
        f"{first} Open the link, sign in to Joplin as {email} if it asks, press Authorise, "
        "then ask again. The link works once and expires in 30 minutes.",
        connect_url=_confirm_url(aid),
    )


def _open_session(sub: str, email: str, cred: dict) -> UserApi:
    """Log in with a credential and check whose Joplin account it opens (D9) and what it holds.
    Chat keeps no credential it will not use: one that fails a check is removed in Joplin too."""
    api = UserApi(cred["app_id"], cred["password"], on_revoked=lambda: _forget(sub))
    try:
        if api.account_email().strip().lower() != email.strip().lower():
            raise Refused(
                "joplin_account_mismatch",
                f"The Joplin account that pressed Authorise is not the one of {email}, so Chat "
                f"did not keep the connection. Sign out of {JOPLIN_PUBLIC_URL}, sign in as {email}, "
                "then call joplin_connect again.",
            )
        api.check_sync_target()
    except Refused:
        try:
            api.revoke_self()
        except Exception:
            log.warning("could not remove a refused application in Joplin")
        raise
    return api


def _cache(sub: str, email: str, api: UserApi) -> UserApi:
    with _registry_lock:
        _apis[sub] = (api, time.monotonic(), email.lower())
    return api


def _user_api(sub: str, email: str) -> UserApi:
    """This person's live Joplin session, connecting first if need be. Raises UserFacing."""
    with _registry_lock:
        hit = _apis.get(sub)
    if hit and time.monotonic() - hit[1] < SESSION_MAX_AGE and hit[2] == email.lower():
        return hit[0]

    store = _get_store()
    found = store.get("creds", sub)
    if found:
        cred = found[0]
        if cred.get("email", "").lower() != email.lower():
            # Keycloak now gives another email than the one this credential was issued for.
            _forget(sub)
            raise UserFacing("joplin_account_mismatch",
                             "Your account email changed since Chat was connected to Joplin; "
                             "call joplin_connect to connect again.")
        try:
            api = _open_session(sub, email, cred)
        except Revoked as e:
            raise UserFacing("joplin_revoked", str(e)) from None
        except Refused:
            _forget(sub)
            raise
        return _cache(sub, email, api)

    pending = store.get("pending", sub)
    if pending and time.time() - pending[1] < PENDING_TTL:
        aid = pending[0]["aid"]
        r = requests.get(f"{JOPLIN_URL}/api/application_auth/{aid}", headers=JOPLIN_HEADERS,
                         timeout=HTTP_TIMEOUT)
        if r.status_code == 403:
            raise _not_connected(aid, email, waiting=True)
        r.raise_for_status()
        j = r.json()
        # Joplin hands the credential out once: from here on it exists only in this process.
        store.delete("pending", sub)
        cred = {"app_id": j["id"], "password": j["password"], "email": email.lower()}
        api = _open_session(sub, email, cred)
        store.put("creds", sub, cred)
        return _cache(sub, email, api)

    aid = str(uuid.uuid4())
    store.put("pending", sub, {"aid": aid})
    raise _not_connected(aid, email, waiting=False)


# joplin_server_mcp's tools find their API through _get_api(); here it is the caller's own.
_api_for_call: ContextVar[UserApi | None] = ContextVar("joplin_user_mcp_api", default=None)


def _caller_api() -> UserApi:
    api = _api_for_call.get()
    if api is None:
        raise RuntimeError("no Joplin session bound to this call")
    return api


jsm._get_api = _caller_api


def _invoke(sub: str, email: str, fn, kwargs: dict) -> str:
    with _user_lock(sub):
        api = _user_api(sub, email)
        token = _api_for_call.set(api)
        try:
            return fn(**kwargs)
        finally:
            _api_for_call.reset(token)


def _outcome_of(out: str) -> str:
    try:
        return "tool_error" if "error" in json.loads(out) else "ok"
    except (ValueError, TypeError):
        return "ok"


async def _run(tool: str, fn, kwargs: dict) -> str:
    sub, email, who = _identity()
    t0 = time.monotonic()
    outcome = "ok"
    try:
        out = await asyncio.to_thread(_invoke, sub, email, fn, kwargs)
        outcome = _outcome_of(out) if isinstance(out, str) and out.startswith("{") else "ok"
        return out
    except UserFacing as e:
        outcome = e.code
        return json.dumps(e.payload(), ensure_ascii=False)
    except Exception as e:
        # Never let an exception text (which could quote a note) reach a log.
        outcome = f"error_{type(e).__name__}"
        return json.dumps({"error": "internal_error", "detail": type(e).__name__})
    finally:
        ms = int((time.monotonic() - t0) * 1000)
        log.info("tool=%s user=%s outcome=%s ms=%d", tool, who, outcome, ms)


def _transport_security() -> TransportSecuritySettings:
    """Allow the Host this service is actually reached by (else 421); see mail-mcp."""
    parts = urlsplit(RESOURCE_URL)
    host = parts.hostname or "127.0.0.1"
    allowed = {parts.netloc, f"{host}:*", "127.0.0.1:*", "localhost:*", "[::1]:*"}
    # No allowed_origins: Open WebUI calls server-side and sends no Origin.
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(allowed),
        allowed_origins=[],
    )


mcp = FastMCP(
    "joplin",
    stateless_http=True,
    json_response=True,
    transport_security=_transport_security(),
)
mcp.settings.streamable_http_path = "/mcp"


# ── tools ────────────────────────────────────────────────────────────────────

# joplin_server_mcp's tools offered here. Not here: joplin_delete_note and
# joplin_delete_folder (D3), joplin_ping (it reports the shared account).
EXPOSED = (
    "joplin_create_note",
    "joplin_get_note",
    "joplin_update_note",
    "joplin_list_notes",
    "joplin_search_notes",
    "joplin_create_folder",
    "joplin_list_folders",
    "joplin_create_tag",
    "joplin_list_tags",
    "joplin_add_tag_to_note",
    "joplin_append_to_section",
    "joplin_replace_section",
    "joplin_apply_patch",
)


# Flat arguments (infra-47nq.12). Upstream takes one `params: <Model>` argument, so its schema is
# {"params": {"$ref": "#/$defs/<Model>"}} and Open WebUI hands that to the model unresolved: the
# household model never saw the fields or the response_format enum, sent a value of its own and
# got a validation error back. Here every field is a top-level argument, the schema carries no
# $ref, and response_format is not offered (Chat reads Markdown).
#
# Still accepted, never advertised: response_format ("json" is what the guard probe parses;
# anything else means Markdown), and the old nested {"params": {...}} shape, which older chats
# still hold in their history. Empty or null optional fields count as not given.
HIDDEN = ("response_format",)


def _passthrough(value, _handler):
    return value


# FastMCP validates arguments against a model built from the signature, and JSON-decodes string
# values of any argument not annotated exactly `str`. Raw keeps both away from what the model
# sent (a body that happens to be a JSON text stays text); the upstream model validates instead.
Raw = Annotated[str, WrapValidator(_passthrough)]


def _is_text(field) -> bool:
    return field.annotation in (str, Optional[str])


def _flat_schema(model) -> dict:
    full = model.model_json_schema()
    defs = full.get("$defs", {})

    def tidy(node):
        if isinstance(node, list):
            return [tidy(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            rest = {k: v for k, v in node.items() if k != "$ref"}
            node = {**defs[node["$ref"].rsplit("/", 1)[-1]], **rest}
        options = node.get("anyOf")
        if isinstance(options, list) and len(options) == 2 and {"type": "null"} in options:
            # Optional[X] = None: say X; leaving the field out is how to give no value.
            rest = {k: v for k, v in node.items() if k != "anyOf" and not (k == "default" and v is None)}
            node = {**next(o for o in options if o != {"type": "null"}), **rest}
        # "title" is pydantic's label for a schema; under "properties" it is a field name.
        return {k: ({p: tidy(s) for p, s in v.items()} if k == "properties" else tidy(v))
                for k, v in node.items() if k != "title"}

    props = {k: tidy(v) for k, v in full["properties"].items() if k not in HIDDEN}
    return {"type": "object", "properties": props,
            "required": [k for k in full.get("required", []) if k not in HIDDEN]}


def _arguments(model, sent: dict) -> dict:
    args = {}
    legacy = sent.get("params")
    if isinstance(legacy, str):
        try:
            legacy = json.loads(legacy)
        except ValueError:
            legacy = None
    if isinstance(legacy, dict):
        args.update(legacy)
    args.update({k: v for k, v in sent.items() if k != "params" and v is not None})
    out = {}
    for name, field in model.model_fields.items():
        value = args.get(name)
        if value is None:
            continue
        if value == "" and not field.is_required() and (field.default is None or not _is_text(field)):
            continue
        if name == "response_format" and value not in ("markdown", "json"):
            continue
        if isinstance(value, str) and not _is_text(field):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, (list, dict)):
                value = parsed  # e.g. apply_patch operations sent as a JSON text
        out[name] = value
    return out


def _invalid(tool: str, e: ValidationError) -> str:
    # pydantic's msg never quotes the input, so this neither leaks a note nor needs one.
    _, _, who = _identity()
    log.info("tool=%s user=%s outcome=invalid_arguments ms=0", tool, who)
    detail = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
                       for err in e.errors())
    return json.dumps({"error": "invalid_arguments", "detail": detail}, ensure_ascii=False)


def _describe(doc: str) -> str:
    """The upstream docstring without its "Args: params: <Model> ..." block."""
    out, skipping = [], False
    for line in (doc or "").splitlines():
        if line.strip() == "Args:":
            skipping = True
            continue
        if skipping and line[:1].isspace() and line.strip():
            continue
        skipping = False
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _expose(source) -> None:
    fn = source.fn
    model = inspect.signature(fn).parameters["params"].annotation

    async def tool(**sent) -> str:
        try:
            params = model.model_validate(_arguments(model, sent))
        except ValidationError as e:
            return _invalid(source.name, e)
        return await _run(source.name, fn, {"params": params})

    tool.__name__ = source.name
    tool.__signature__ = inspect.Signature(
        [inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Raw)
         for n in (*model.model_fields, "params")],
        return_annotation=str)
    mcp.add_tool(tool, name=source.name, description=_describe(source.description),
                 annotations=source.annotations)
    mcp._tool_manager.get_tool(source.name).parameters = _flat_schema(model)


for _t in jsm.mcp._tool_manager.list_tools():
    if _t.name in EXPOSED:
        _expose(_t)


def _connect_status(sub: str, email: str) -> dict:
    with _user_lock(sub):
        _user_api(sub, email)
    return {"connected": True, "joplin": JOPLIN_PUBLIC_URL, "account": email,
            "message": "Chat is connected to your own Joplin notes."}


def _disconnect(sub: str, email: str) -> dict:
    with _user_lock(sub):
        store = _get_store()
        found = store.get("creds", sub)
        store.delete("pending", sub)
        revoked = False
        if found:
            with _registry_lock:
                hit = _apis.get(sub)
            try:
                api = hit[0] if hit else UserApi(found[0]["app_id"], found[0]["password"])
                revoked = api.revoke_self()
            except Revoked:
                revoked = True  # already removed in Joplin
            except Exception:
                revoked = False
        _forget(sub)
    if not found:
        return {"connected": False, "message": "Chat was not connected to your Joplin notes."}
    if revoked:
        return {"connected": False, "message": "Disconnected: Chat forgot the credential and removed it in Joplin."}
    return {"connected": False,
            "message": f"Chat forgot the credential. Joplin did not confirm its removal: delete the "
                       f"'vchat.mdapi.ch' entry on {JOPLIN_PUBLIC_URL}/applications yourself."}


async def _run_own(tool: str, fn) -> str:
    sub, email, who = _identity()
    t0 = time.monotonic()
    outcome = "ok"
    try:
        return json.dumps(await asyncio.to_thread(fn, sub, email), ensure_ascii=False)
    except UserFacing as e:
        outcome = e.code
        return json.dumps(e.payload(), ensure_ascii=False)
    except Exception as e:
        outcome = f"error_{type(e).__name__}"
        return json.dumps({"error": "internal_error", "detail": type(e).__name__})
    finally:
        log.info("tool=%s user=%s outcome=%s ms=%d", tool, who, outcome, int((time.monotonic() - t0) * 1000))


@mcp.tool(annotations={"title": "Connect my Joplin notes", "readOnlyHint": True})
async def joplin_connect() -> str:
    """Connect Chat to my own Joplin notes (notes.mdapi.ch), or check that it is connected.
    When it is not connected yet, this returns a link: give it to me, I open it, sign in to
    Joplin with my own account and press Authorise, then call this again. The other joplin_*
    tools return the same link while the connection is missing."""
    return await _run_own("joplin_connect", _connect_status)


@mcp.tool(annotations={"title": "Disconnect my Joplin notes", "readOnlyHint": False, "destructiveHint": False})
async def joplin_disconnect() -> str:
    """Disconnect Chat from my Joplin notes: Chat forgets its credential and removes it in
    Joplin (the 'vchat.mdapi.ch' entry on notes.mdapi.ch/applications). My notes are not
    touched. Use it only when I ask to disconnect."""
    return await _run_own("joplin_disconnect", _disconnect)


# ── HTTP surface ─────────────────────────────────────────────────────────────

class AuthMiddleware:
    """Validate the bearer token on the MCP endpoint; 401 (MCP-style) otherwise. See mail-mcp."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith("/.well-known/") or path in ("/healthz", "/readyz"):
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return await self._unauthorized(send)

        try:
            claims = await asyncio.to_thread(_validate_incoming, auth.split(" ", 1)[1].strip())
        except AuthError as e:
            log.warning("auth refused: %s", e)
            return await self._unauthorized(send)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("auth error: %s", type(e).__name__)
            return await self._unauthorized(send)

        who = hashlib.sha256(claims["sub"].encode()).hexdigest()[:12]
        reset = _current.set((claims["sub"], claims["email"], who))
        try:
            return await self.app(scope, receive, send)
        finally:
            _current.reset(reset)

    async def _unauthorized(self, send: Send):
        body = json.dumps({"error": "invalid_token", "error_description": "Auth required"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", f'Bearer resource_metadata="{METADATA_URL}"'.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


app = mcp.streamable_http_app()


async def _prm(request):
    return JSONResponse(
        {
            "resource": RESOURCE_URL,
            "authorization_servers": [KEYCLOAK_ISSUER],
            "scopes_supported": ["openid", "profile", "email"],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://chat.mdapi.ch",
        }
    )


async def _health(request):
    return PlainTextResponse("ok")


def _add(path: str, endpoint):
    from starlette.routing import Route

    app.router.routes.insert(0, Route(path, endpoint))


_add("/.well-known/oauth-protected-resource", _prm)
_add("/.well-known/oauth-protected-resource/mcp", _prm)
_add("/healthz", _health)
_add("/readyz", _health)
app.add_middleware(AuthMiddleware)


if __name__ == "__main__":
    # force: FastMCP() already installed a root handler at import time.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    # requests/urllib3 would log request URLs, which carry note ids.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _get_store()  # refuse to start without STORE_KEY or a writable store
    log.info("serving MCP on :%d/mcp resource=%s issuer=%s joplin=%s", PORT, RESOURCE_URL, KEYCLOAK_ISSUER, JOPLIN_URL)
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
