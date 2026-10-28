"""Tests for joplin_user_mcp: no network, no Joplin, no Keycloak.

    pip install pytest "PyJWT[crypto]" cryptography uvicorn requests httpx "mcp>=1.0.0,<2.0.0" joppy
    python -m pytest tests/
"""

import contextlib
import json
import logging
import os
import sys
import time
import types

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric import rsa

os.environ.setdefault("STORE_KEY", Fernet.generate_key().decode())
os.environ.setdefault("ALLOWED_GROUPS", "ChatNotes")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jwt  # noqa: E402
import pytest  # noqa: E402
from joppy import server_api as joppy_server_api  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import joplin_server_mcp as jsm  # noqa: E402
import joplin_user_mcp as jum  # noqa: E402

A = ("sub-a", "a@example.test")
B = ("sub-b", "b@example.test")


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setattr(jum, "_store", jum.Store(str(tmp_path / "store.sqlite3"), os.environ["STORE_KEY"]))
    monkeypatch.setattr(jum, "_apis", {})
    monkeypatch.setattr(jum, "_user_locks", {})


# ── fakes ────────────────────────────────────────────────────────────────────

class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code, self._body = status, body
        self.text = text if text is not None else json.dumps(body if body is not None else {})
        self.ok = status < 400
        self.encoding = None

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            import requests

            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


class FakeHttp:
    """Stands in for one requests.Session talking to a Joplin account."""

    def __init__(self, user_id="u1", email="a@example.test", info=None, login_status=200):
        self.headers, self.calls = {}, []
        self.user_id, self.email, self.login_status = user_id, email, login_status
        self.info = info if info is not None else {"version": 3, "e2ee": {"value": False}}
        self.forbid_next_get = 0
        self.logins = 0

    def post(self, url, **kw):
        self.calls.append(("post", url))
        if url.endswith("/api/sessions"):
            self.logins += 1
            if self.login_status != 200:
                return Resp(self.login_status, {"error": "x"})
            return Resp(200, {"id": f"sess-{self.logins}", "user_id": self.user_id})
        if url.endswith("/delete"):
            return Resp(302, text="")
        return Resp(200, {})

    def get(self, url, **kw):
        self.calls.append(("get", url))
        if self.forbid_next_get:
            self.forbid_next_get -= 1
            return Resp(403, {"error": "forbidden"})
        if "/api/users/" in url:
            return Resp(200, {"id": self.user_id, "email": self.email})
        if "info.json" in url:
            return Resp(200, self.info)
        if url.endswith("/applications"):
            return Resp(200, text='<form><input type="hidden" name="_csrf" value="tok"></form>')
        return Resp(200, {"items": [], "has_more": False})

    def put(self, url, **kw):
        self.calls.append(("put", url))
        return Resp(200, {})


@pytest.fixture
def no_shared_session(monkeypatch):
    """joppy's module-level SESSION must never be used."""

    def boom(*a, **k):
        raise AssertionError("joppy's shared SESSION was used")

    for m in ("get", "post", "put", "delete"):
        monkeypatch.setattr(joppy_server_api.SESSION, m, boom)


# ── the tool set ─────────────────────────────────────────────────────────────

def _tools():
    return {t.name: t for t in jum.mcp._tool_manager.list_tools()}


def test_tool_set_has_no_deletes_and_no_shared_ping():
    names = set(_tools())
    assert names == set(jum.EXPOSED) | {"joplin_connect", "joplin_disconnect"}
    assert not any("delete" in n for n in jum.EXPOSED)
    assert "joplin_ping" not in names


def _upstream():
    return {t.name: t for t in jsm.mcp._tool_manager.list_tools()}


def _model(name):
    return _upstream()[name].fn.__annotations__["params"]


def test_exposed_tools_keep_the_upstream_fields_and_annotations():
    ours, theirs = _tools(), _upstream()
    for name in jum.EXPOSED:
        model = _model(name)
        schema = ours[name].parameters
        assert set(schema["properties"]) == set(model.model_fields) - set(jum.HIDDEN), name
        assert schema["required"] == [n for n, f in model.model_fields.items() if f.is_required()], name
        assert ours[name].annotations == theirs[name].annotations, name
        assert ours[name].description.split("\n")[0] == theirs[name].description.split("\n")[0], name


def test_exposed_schemas_are_flat_for_small_models():
    # infra-47nq.12: Open WebUI passes inputSchema unresolved, so a $ref hides every field.
    for name in jum.EXPOSED:
        tool = _tools()[name]
        text = json.dumps(tool.parameters)
        assert "$ref" not in text and "$defs" not in text, name
        assert "params" not in tool.parameters["properties"], name
        assert "response_format" not in text, name
        assert '"null"' not in text, name
        assert "params:" not in tool.description and "Args:" not in tool.description, name
    patch = _tools()["joplin_apply_patch"].parameters["properties"]["operations"]
    assert patch["items"]["required"] == ["search"]


def test_arguments_take_what_small_models_send():
    ln, gn, up = _model("joplin_list_notes"), _model("joplin_get_note"), _model("joplin_update_note")
    assert jum._arguments(ln, {"folder_id": "", "response_format": "text"}) == {}
    assert jum._arguments(ln, {"folder_id": None, "response_format": "json"}) == {"response_format": "json"}
    assert jum._arguments(gn, {"params": {"note_id": "n1", "response_format": "json"}}) == \
        {"note_id": "n1", "response_format": "json"}
    assert jum._arguments(gn, {"params": '{"note_id": "n1"}'}) == {"note_id": "n1"}
    assert jum._arguments(gn, {"params": {"note_id": "old"}, "note_id": "new"}) == {"note_id": "new"}
    assert jum._arguments(gn, {"note_id": "n1", "limit": 5}) == {"note_id": "n1"}
    # An empty optional text field is "not given": it must not blank a title.
    assert jum._arguments(up, {"note_id": "n1", "title": "", "body": "x", "force": ""}) == \
        {"note_id": "n1", "body": "x"}
    # A body that is a JSON text stays text; a list sent as JSON text becomes a list.
    cn, ap = _model("joplin_create_note"), _model("joplin_apply_patch")
    assert jum._arguments(cn, {"title": "t", "parent_id": "p", "body": '{"a": 1}'})["body"] == '{"a": 1}'
    ops = jum._arguments(ap, {"note_id": "n1", "operations": '[{"search": "a", "replace": "b"}]'})["operations"]
    assert ops == [{"search": "a", "replace": "b"}]


# ── the per-call API seam ────────────────────────────────────────────────────

def test_old_tools_resolve_the_callers_api(monkeypatch):
    apis = {A[0]: object(), B[0]: object()}
    monkeypatch.setattr(jum, "_user_api", lambda sub, email: apis[sub])
    assert jum._invoke(*A, lambda: jsm._get_api(), {}) is apis[A[0]]
    assert jum._invoke(*B, lambda: jsm._get_api(), {}) is apis[B[0]]
    with pytest.raises(RuntimeError):
        jsm._get_api()  # outside a call there is no API, and never the shared account


def test_a_real_old_tool_runs_on_the_callers_api(monkeypatch):
    class Api:
        def __init__(self, title):
            self.title = title

        @contextlib.contextmanager
        def sync_lock(self):
            yield

        def get_all_notebooks(self):
            return [types.SimpleNamespace(id="n", title=self.title, parent_id="",
                                          created_time=None, updated_time=None)]

    apis = {A[0]: Api("folder of A"), B[0]: Api("folder of B")}
    monkeypatch.setattr(jum, "_user_api", lambda sub, email: apis[sub])
    fn = _upstream()["joplin_list_folders"].fn
    params = jsm.ListFoldersInput(response_format="json")
    out_a = json.loads(jum._invoke(*A, fn, {"params": params}))
    out_b = json.loads(jum._invoke(*B, fn, {"params": params}))
    assert [f["title"] for f in out_a["folders"]] == ["folder of A"]
    assert [f["title"] for f in out_b["folders"]] == ["folder of B"]


# ── the store ────────────────────────────────────────────────────────────────

def test_store_holds_only_ciphertext_under_an_hmac(tmp_path):
    key = Fernet.generate_key().decode()
    path = str(tmp_path / "s.sqlite3")
    s = jum.Store(path, key)
    s.put("creds", "subject-123", {"app_id": "APPID", "password": "SECRETPW", "email": "who@example.test"})
    assert s.get("creds", "subject-123")[0]["password"] == "SECRETPW"
    raw = open(path, "rb").read() + (open(path + "-wal", "rb").read() if os.path.exists(path + "-wal") else b"")
    for plain in (b"subject-123", b"APPID", b"SECRETPW", b"who@example.test"):
        assert plain not in raw
    other = jum.Store(path, Fernet.generate_key().decode())
    assert other.get("creds", "subject-123") is None  # another key derives another row key
    row = s._db.execute("SELECT blob FROM creds").fetchone()[0]
    with pytest.raises(InvalidToken):
        Fernet(Fernet.generate_key()).decrypt(row)
    s.delete("creds", "subject-123")
    assert s.get("creds", "subject-123") is None


# ── incoming tokens ──────────────────────────────────────────────────────────

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signed(monkeypatch):
    monkeypatch.setattr(jum, "jwks", types.SimpleNamespace(
        get_signing_key_from_jwt=lambda t: types.SimpleNamespace(key=_KEY.public_key())))

    def make(**over):
        claims = {"iss": jum.KEYCLOAK_ISSUER, "aud": jum.EXPECTED_AUDIENCE, "azp": jum.EXPECTED_AZP,
                  "sub": A[0], "email": A[1], "email_verified": True, "groups": ["ChatNotes"],
                  "exp": int(time.time()) + 300}
        claims.update(over)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, _KEY, algorithm="RS256")

    return make


def test_a_member_is_let_in(signed):
    assert jum._validate_incoming(signed())["sub"] == A[0]


@pytest.mark.parametrize("over", [
    {"groups": ["ChatMail"]},
    {"groups": None},
    {"azp": "openwebui"},
    {"aud": "openwebui-mail-mcp"},
    {"email_verified": False},
    {"email": None},
    {"iss": "https://idp.example.test/realms/other"},
    {"exp": int(time.time()) - 10},
])
def test_refusals(signed, over):
    with pytest.raises(jum.AuthError):
        jum._validate_incoming(signed(**over))


def test_empty_allowed_groups_refuses_everybody(signed, monkeypatch):
    monkeypatch.setattr(jum, "ALLOWED_GROUPS", [])
    with pytest.raises(jum.AuthError):
        jum._validate_incoming(signed())


# ── one person's Joplin client ───────────────────────────────────────────────

def test_each_user_api_has_its_own_http_session(monkeypatch, no_shared_session):
    made = []
    monkeypatch.setattr(jum, "_new_http", lambda: made.append(FakeHttp()) or made[-1])
    a, b = jum.UserApi("app-a", "pw-a"), jum.UserApi("app-b", "pw-b")
    assert a._http is not b._http
    assert a._http.headers["X-API-AUTH"] != "" and "X-API-AUTH" in b._http.headers
    a.get("/api/items/root:/info.json:/content")
    assert ("get", f"{jum.JOPLIN_URL}/api/items/root:/info.json:/content?") in a._http.calls


def test_user_api_creates_a_missing_info_json(monkeypatch, no_shared_session):
    http = FakeHttp()
    real_get = http.get
    http.get = lambda url, **kw: Resp(404, {"error": "nf"}) if ("info.json" in url or "version.txt" in url) else real_get(url, **kw)
    monkeypatch.setattr(jum, "_new_http", lambda: http)
    jum.UserApi("app", "pw")
    assert ("put", f"{jum.JOPLIN_URL}/api/items/root:/info.json:/content?") in http.calls


def test_a_403_on_an_older_session_logs_in_once_more(monkeypatch, no_shared_session):
    http = FakeHttp()
    monkeypatch.setattr(jum, "_new_http", lambda: http)
    api = jum.UserApi("app", "pw")
    http.forbid_next_get = 1
    with pytest.raises(Exception):
        api.get("/api/items/root:/info.json:/content")  # young session: no re-login, the 403 stands
    assert http.logins == 1
    api.logged_in_at -= jum.RELOGIN_MIN_AGE + 1
    http.forbid_next_get = 1
    api.get("/api/items/root:/info.json:/content")
    assert http.logins == 2


def test_a_revoked_credential_is_forgotten(monkeypatch, no_shared_session):
    jum._get_store().put("creds", A[0], {"app_id": "app", "password": "pw", "email": A[1]})
    monkeypatch.setattr(jum, "_new_http", lambda: FakeHttp(login_status=403))
    with pytest.raises(jum.UserFacing) as e:
        jum._user_api(*A)
    assert e.value.code == "joplin_revoked"
    assert jum._get_store().get("creds", A[0]) is None


def test_rate_limited_login_keeps_the_credential(monkeypatch, no_shared_session):
    jum._get_store().put("creds", A[0], {"app_id": "app", "password": "pw", "email": A[1]})
    monkeypatch.setattr(jum, "_new_http", lambda: FakeHttp(login_status=429))
    with pytest.raises(jum.UserFacing) as e:
        jum._user_api(*A)
    assert e.value.code == "joplin_busy"
    assert jum._get_store().get("creds", A[0]) is not None


# ── connecting ───────────────────────────────────────────────────────────────

def _application_auth(monkeypatch, status, body=None):
    polled = []

    def get(url, **kw):
        polled.append(url)
        assert kw.get("headers") == jum.JOPLIN_HEADERS
        return Resp(status, body or {"error": "Application not authorized yet."})

    monkeypatch.setattr(jum.requests, "get", get)
    return polled


def test_in_cluster_requests_name_the_public_host():
    # Joplin Server refuses any other Host with 404 "Invalid origin" (seen live, 3.7.3).
    assert jum.JOPLIN_URL.startswith("http://joplin.joplin.svc.cluster.local")
    assert jum.JOPLIN_HEADERS == {"Host": "notes.mdapi.ch"}
    assert jum._new_http().headers["Host"] == "notes.mdapi.ch"


def test_first_call_hands_out_a_confirm_link_then_waits(monkeypatch):
    polled = _application_auth(monkeypatch, 403)
    with pytest.raises(jum.UserFacing) as first:
        jum._user_api(*A)
    url = first.value.extra["connect_url"]
    assert url.startswith(f"{jum.JOPLIN_PUBLIC_URL}/applications/")
    assert url.endswith("/confirm?version=chat.mdapi.ch&platform=6&type=3")
    assert len("chat.mdapi.ch") <= 16  # applications.version is varchar(16)
    with pytest.raises(jum.UserFacing) as second:
        jum._user_api(*A)
    assert second.value.extra["connect_url"] == url and "still waiting" in second.value.message
    assert len(polled) == 1 and url.split("/")[4] in polled[0]


def test_an_authorised_link_is_stored_for_that_person_only(monkeypatch, no_shared_session):
    with pytest.raises(jum.UserFacing):
        jum._user_api(*A)
    _application_auth(monkeypatch, 200, {"id": "app-a", "password": "pw-a", "status": "finished"})
    monkeypatch.setattr(jum, "_new_http", lambda: FakeHttp(email=A[1]))
    api = jum._user_api(*A)
    assert api.user == "app-a"
    assert jum._get_store().get("creds", A[0])[0]["app_id"] == "app-a"
    assert jum._get_store().get("pending", A[0]) is None
    assert jum._user_api(*A) is api  # cached
    with pytest.raises(jum.UserFacing) as e:
        jum._user_api(*B)  # B has nothing yet: B gets a link of its own, never A's session
    assert e.value.code == "joplin_not_connected"


@pytest.mark.parametrize("joplin_email", ["b@example.test", ""])
def test_d9_another_joplin_account_fails_closed_and_is_revoked(monkeypatch, no_shared_session, joplin_email):
    with pytest.raises(jum.UserFacing):
        jum._user_api(*A)
    _application_auth(monkeypatch, 200, {"id": "app-x", "password": "pw-x", "status": "finished"})
    http = FakeHttp(email=joplin_email)
    monkeypatch.setattr(jum, "_new_http", lambda: http)
    with pytest.raises(jum.Refused) as e:
        jum._user_api(*A)
    assert e.value.code == "joplin_account_mismatch"
    assert jum._get_store().get("creds", A[0]) is None
    assert ("post", f"{jum.JOPLIN_URL}/applications/app-x/delete") in http.calls


def test_e2ee_account_is_refused_and_revoked(monkeypatch, no_shared_session):
    with pytest.raises(jum.UserFacing):
        jum._user_api(*A)
    _application_auth(monkeypatch, 200, {"id": "app-e", "password": "pw-e", "status": "finished"})
    http = FakeHttp(email=A[1], info={"version": 3, "e2ee": {"value": True}})
    monkeypatch.setattr(jum, "_new_http", lambda: http)
    with pytest.raises(jum.Refused) as e:
        jum._user_api(*A)
    assert e.value.code == "joplin_encrypted"
    assert jum._get_store().get("creds", A[0]) is None
    assert ("post", f"{jum.JOPLIN_URL}/applications/app-e/delete") in http.calls


def test_a_changed_token_email_drops_the_stored_credential(monkeypatch):
    jum._get_store().put("creds", A[0], {"app_id": "app", "password": "pw", "email": "old@example.test"})
    with pytest.raises(jum.UserFacing) as e:
        jum._user_api(*A)
    assert e.value.code == "joplin_account_mismatch"
    assert jum._get_store().get("creds", A[0]) is None


def test_disconnect_revokes_in_joplin_and_forgets(monkeypatch, no_shared_session):
    http = FakeHttp(email=A[1])
    monkeypatch.setattr(jum, "_new_http", lambda: http)
    jum._get_store().put("creds", A[0], {"app_id": "app-a", "password": "pw", "email": A[1]})
    jum._user_api(*A)
    out = jum._disconnect(*A)
    assert out["connected"] is False and "removed it in Joplin" in out["message"]
    assert ("post", f"{jum.JOPLIN_URL}/applications/app-a/delete") in http.calls
    assert jum._get_store().get("creds", A[0]) is None and A[0] not in jum._apis


# ── over HTTP, through the real MCP stack ────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    # The MCP session manager starts once per process, so the HTTP tests share one app run.
    with TestClient(jum.app, base_url="http://localhost:8080") as c:
        yield c


def _rpc(client, token, name, arguments=None):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    return client.post("/mcp", headers=headers, json=body)


def test_http_without_a_token_is_401_with_resource_metadata(client):
    r = _rpc(client, None, "joplin_connect")
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    assert "error=" not in r.headers["www-authenticate"]  # RFC 6750 3.1: no token, no error code
    assert client.get("/healthz").status_code == 200
    prm = client.get("/.well-known/oauth-protected-resource").json()
    assert prm["authorization_servers"] == [jum.KEYCLOAK_ISSUER]


def test_http_non_member_is_401(client, signed):
    assert _rpc(client, signed(groups=["ChatMail"]), "joplin_connect").status_code == 401


def test_http_rejected_token_is_401_invalid_token(client, signed):
    for token in ("not-a-jwt", signed(groups=["ChatMail"])):
        r = _rpc(client, token, "joplin_connect")
        assert r.status_code == 401
        www = r.headers["www-authenticate"]
        assert www.startswith("Bearer ") and 'error="invalid_token"' in www and "resource_metadata=" in www
        assert r.json()["error"] == "invalid_token"


def test_http_tool_calls_run_as_the_token_holder(client, signed, monkeypatch, caplog):
    seen = []

    def fake_user_api(sub, email):
        seen.append((sub, email))
        return object()

    monkeypatch.setattr(jum, "_user_api", fake_user_api)
    caplog.set_level(logging.INFO, logger="joplin_user_mcp")
    for sub, email in (A, B):
        r = _rpc(client, signed(sub=sub, email=email), "joplin_connect")
        assert r.status_code == 200, r.text
        text = r.json()["result"]["content"][0]["text"]
        assert json.loads(text)["account"] == email
    assert seen == [A, B]
    logged = caplog.text
    assert "tool=joplin_connect" in logged
    assert A[1] not in logged and B[1] not in logged and A[0] not in logged


def test_http_flat_legacy_and_bad_arguments(client, signed, monkeypatch, caplog):
    got = []

    async def fake_run(tool, fn, kwargs):
        got.append((tool, kwargs["params"]))
        return "ok"

    monkeypatch.setattr(jum, "_run", fake_run)
    caplog.set_level(logging.INFO, logger="joplin_user_mcp")
    tok = signed()

    def call(name, arguments):
        r = _rpc(client, tok, name, arguments)
        assert r.status_code == 200, r.text
        return r.json()["result"]

    # What the household model sent (a made-up response_format) now lists the notes.
    assert call("joplin_list_notes", {"response_format": "list"})["content"][0]["text"] == "ok"
    assert call("joplin_get_note", {"note_id": "n1"})["content"][0]["text"] == "ok"
    assert call("joplin_search_notes", {"params": {"query": "q", "response_format": "json"}})["content"][0]["text"] == "ok"
    assert [(t, p.model_dump(mode="json")) for t, p in got] == [
        ("joplin_list_notes", {"folder_id": None, "response_format": "markdown"}),
        ("joplin_get_note", {"note_id": "n1", "response_format": "markdown"}),
        ("joplin_search_notes", {"query": "q", "response_format": "json"}),
    ]
    bad = json.loads(call("joplin_get_note", {"note_id": None, "title": "secret title"})["content"][0]["text"])
    assert bad == {"error": "invalid_arguments", "detail": "note_id: Field required"}
    assert len(got) == 3
    assert "tool=joplin_get_note" in caplog.text and "outcome=invalid_arguments" in caplog.text
    assert "secret title" not in caplog.text
