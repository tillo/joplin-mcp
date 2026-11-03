"""Shared fakes. A loopback Joplin Server on 127.0.0.1; no real Joplin, no real notes.

Every item here is made up in the test that needs it.
"""

import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

# Imported before joplin_user_mcp, which rebinds jsm._get_api to the per-call API at import:
# the bearer (single-account) mode runs this original.
import joplin_server_mcp as jsm  # noqa: E402

SERVER_GET_API = jsm._get_api

FOLDER = "f" * 32
EMAIL = "owner@example.test"
PASSWORD = "not-a-real-password"

_PY_TIME = re.compile(r"^(\w+): (\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)(?:\.(\d{3})\d*)?$", re.M)


def _iso_times(text: str) -> str:
    """Joplin Server keeps an item's fields and serves its times back as ISO 8601. joppy writes
    Python's str(datetime), which its own reader does not parse, so the fake does what the
    server does. Only the metadata block (after the last blank line) is touched."""
    head, sep, meta = text.rpartition("\n\n")
    return head + sep + _PY_TIME.sub(lambda m: f"{m[1]}: {m[2]}T{m[3]}.{m[4] or '000'}Z", meta)


def note_text(id_, title, body=None, parent_id=FOLDER, is_todo=0, todo_completed=0, extra=()):
    """A note as a Joplin desktop client stores it on the server."""
    head = [title, ""] if title is not None else []
    if body is not None:
        head += [body, ""]
    meta = [f"id: {id_}", f"parent_id: {parent_id}",
            "created_time: 2026-01-02T03:04:05.678Z", "updated_time: 2026-01-03T04:05:06.789Z",
            "is_conflict: 0", "author: ", f"is_todo: {is_todo}", "todo_due: 0",
            f"todo_completed: {todo_completed}", "source_application: net.example.desktop",
            "markup_language: 1", "is_shared: 0", "encryption_applied: 0", *extra, "type_: 1"]
    return "\n".join(head + meta)


def folder_text(id_, title, parent_id=""):
    return "\n".join([title, "", f"id: {id_}", f"parent_id: {parent_id}",
                      "created_time: 2026-01-02T03:04:05.678Z",
                      "updated_time: 2026-01-02T03:04:05.678Z", "type_: 2"])


def tag_text(id_, title):
    return "\n".join([title, "", f"id: {id_}", "created_time: 2026-01-02T03:04:05.678Z",
                      "updated_time: 2026-01-02T03:04:05.678Z", "type_: 5"])


class FakeJoplin:
    """Joplin Server as joppy and joplin_user_mcp's UserApi talk to it: items by path, sync
    locks, a login, a ping, and the per-user applications flow."""

    def __init__(self):
        self.items = {"info.json": json.dumps({"version": 3, "e2ee": {"value": False}})}
        self.locks = {}
        self.ping_status = 200
        self.fail = {}  # (METHOD, item name) -> status
        self.requests = []  # (METHOD, path)
        self.logins = 0
        self.apps = {}  # app id -> password, once authorised
        self.pending = {}  # application_auth id -> app id, once the person pressed Authorise
        self.account_email = "a@example.test"
        self.lock = threading.Lock()
        handler = type("Handler", (_Handler,), {"fake": self})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def put(self, name, text):
        self.items[name] = text

    def add_note(self, n, title="Note", body=None, **kw):
        id_ = f"{n:032x}"
        self.put(f"{id_}.md", note_text(id_, title, body, **kw))
        return id_

    def body(self, id_):
        return self.items[f"{id_}.md"]

    def writes(self):
        return [p for m, p in self.requests if m == "PUT" and "/api/items/" in p]

    # ── routes ──────────────────────────────────────────────────────────────

    def route(self, method, path, query, payload):
        self.requests.append((method, path))
        m = re.fullmatch(r"/api/items/root:/(.*):/content", path)
        if m:
            return self._content(method, m[1], payload)
        if path == "/api/items/root:/:/children":
            names = sorted(n for n in self.items if n.endswith(".md"))
            return 200, {"items": [{"name": n} for n in names], "has_more": False, "cursor": None}
        m = re.fullmatch(r"/api/items/root:/(.*):", path)
        if m and method == "DELETE":
            return self._delete(m[1])
        if path.startswith("/api/locks"):
            return self._locks(method, path, payload)
        return self._account(method, path, payload)

    def _content(self, method, name, payload):
        status = self.fail.get((method, name))
        if status:
            return status, {"error": "injected"}
        if method == "PUT":
            text = payload.decode("utf-8")
            self.items[name] = _iso_times(text) if name.endswith(".md") else text
            return 200, {"name": name}
        if name not in self.items:
            return 404, {"error": "Not found"}
        return 200, ("text/plain", self.items[name])

    def _delete(self, name):
        if self.items.pop(name, None) is None:
            return 404, {"error": "Not found"}
        return 200, {}

    def _locks(self, method, path, payload):
        if method == "GET":
            return 200, {"items": list(self.locks.values()), "has_more": False, "cursor": None}
        if method == "POST":
            j = json.loads(payload)
            key = f"{j['type']}_{j['clientType']}_{j['clientId']}"
            self.locks[key] = {"id": uuid.uuid4().hex, "type": j["type"], "clientId": j["clientId"],
                               "clientType": j["clientType"], "updatedTime": int(time.time() * 1000)}
            return 200, self.locks[key]
        self.locks.pop(path.rsplit("/", 1)[1], None)
        return 200, {}

    def _account(self, method, path, payload):
        if path == "/login" and method == "POST":
            self.logins += 1
            return 200, {}
        if path == "/api/ping":
            return self.ping_status, {"status": "ok", "message": "Joplin Server is running"}
        if path == "/api/sessions" and method == "POST":
            j = json.loads(payload)
            if self.apps.get(j["email"]) != j["password"]:
                return 403, {"error": "Invalid username or password"}
            self.logins += 1
            return 200, {"id": f"sess{self.logins}", "user_id": "u1"}
        if path == "/api/users/u1":
            return 200, {"id": "u1", "email": self.account_email}
        m = re.fullmatch(r"/api/application_auth/(.+)", path)
        if m:
            app = self.pending.get(m[1])
            if app is None:
                return 403, {"error": "Application not authorized yet."}
            return 200, {"id": app, "password": self.apps[app], "status": "finished"}
        if path == "/applications":
            return 200, ("text/html", '<form><input type="hidden" name="_csrf" value="csrf1"></form>')
        m = re.fullmatch(r"/applications/(.+)/delete", path)
        if m and method == "POST" and b"_csrf=csrf1" in payload:
            self.apps.pop(m[1], None)
            return 302, ("text/plain", "")
        return 404, {"error": "Not found"}

    def authorise(self, aid, app="app0", password="app-password"):
        """The person pressed Authorise on /applications/<aid>/confirm."""
        self.apps[app] = password
        self.pending[aid] = app


class _Handler(BaseHTTPRequestHandler):
    fake: FakeJoplin

    def log_message(self, *args):
        pass

    def _serve(self):
        parts = urlsplit(self.path)
        size = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(size) if size else b""
        with self.fake.lock:
            status, out = self.fake.route(self.command, parts.path, parts.query, payload)
        ctype, text = out if isinstance(out, tuple) else ("application/json", json.dumps(out))
        data = text.encode("utf-8")
        self.send_response(status)
        # No charset, as Joplin Server sends item content.
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = _serve


@pytest.fixture
def joplin():
    fake = FakeJoplin()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def server_mode(joplin, monkeypatch):
    """joplin_server_mcp as the bearer-mode image runs it: one account, from the environment."""
    monkeypatch.setattr(jsm, "_get_api", SERVER_GET_API)
    monkeypatch.setattr(jsm, "_api", None)
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_URL", joplin.url)
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_EMAIL", EMAIL)
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_PASSWORD", PASSWORD)
    return joplin
