"""Behaviour of joplin_server_mcp's tools, called as an MCP client calls them, against the
loopback Joplin Server in conftest.py (real joppy, real requests, made-up notes).

    pip install -r requirements.txt pytest
    python -m pytest tests/
"""

import asyncio
import json
import os
import uuid

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import EMAIL, FOLDER, folder_text, jsm, note_text, tag_text

GOLDEN = os.path.join(os.path.dirname(__file__), "tools_golden.json")


def call(name, **params):
    args = {} if name == "joplin_ping" else {"params": params}
    content, _ = asyncio.run(jsm.mcp.call_tool(name, args))
    return content[0].text


def call_json(name, **params):
    return json.loads(call(name, **params))


def _meta(text):
    """The metadata block of a stored item, the id line dropped (a new item's id is random)."""
    return [line for line in text.rpartition("\n\n")[2].split("\n") if not line.startswith("id: ")]


# ── the tool surface ─────────────────────────────────────────────────────────

def test_tool_list_is_unchanged():
    tools = {t.name: t.model_dump(mode="json", exclude_none=True)
             for t in asyncio.run(jsm.mcp.list_tools())}
    with open(GOLDEN, encoding="utf-8") as f:
        assert tools == json.load(f)["server"]


INPUTS = [jsm.CreateNoteInput, jsm.UpdateNoteInput, jsm.GetNoteInput, jsm.DeleteNoteInput,
          jsm.ListNotesInput, jsm.SearchNotesInput, jsm.CreateFolderInput, jsm.ListFoldersInput,
          jsm.DeleteFolderInput, jsm.CreateTagInput, jsm.ListTagsInput, jsm.AddTagToNoteInput,
          jsm.AppendToSectionInput, jsm.ReplaceSectionInput, jsm.ApplyPatchInput]

SAMPLE = {"title": "t", "parent_id": FOLDER, "note_id": "n1", "query": "q", "folder_id": "f1",
          "tag_id": "t1", "section_heading": "H", "content": "c", "new_content": "c",
          "operations": [{"search": "a", "replace": "b"}]}


@pytest.mark.parametrize("model", INPUTS, ids=lambda m: m.__name__)
def test_every_input_takes_a_json_text_and_one_stray_closing_brace(model):
    fields = {k: v for k, v in SAMPLE.items() if k in model.model_fields}
    want = model.model_validate(fields)
    text = json.dumps(fields)
    assert model.model_validate(text) == want
    # supergateway may append the outer wrapper's '}'.
    assert model.model_validate(text + "}") == want
    # Anything after the object is cut once the text ends in '}'.
    assert model.model_validate(text + " x}") == want
    for bad in ("{not json", text + "]", text + "} "):
        with pytest.raises(ValueError):
            model.model_validate(bad)


def test_a_json_text_reaches_the_tool(server_mode):
    nid = server_mode.add_note(1, "Plan", "body")
    content, _ = asyncio.run(jsm.mcp.call_tool("joplin_get_note",
                                               {"params": json.dumps({"note_id": nid}) + "}"}))
    assert content[0].text.startswith("# Plan\n")


def test_unknown_arguments_are_refused():
    with pytest.raises(ToolError):
        call("joplin_get_note", note_id="n1", limit=5)


# ── the account ──────────────────────────────────────────────────────────────

def test_missing_credentials_are_reported_by_every_tool(server_mode, monkeypatch):
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_PASSWORD", "")
    assert call_json("joplin_list_tags") == {"error": (
        "JOPLIN_SERVER_EMAIL and JOPLIN_SERVER_PASSWORD must be set. "
        "These are your Joplin Server login credentials.")}
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_PASSWORD", "pw")
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_EMAIL", "")
    assert "must be set" in call_json("joplin_list_tags")["error"]
    assert server_mode.logins == 0


def test_one_login_serves_every_call(server_mode):
    call("joplin_list_tags")
    call("joplin_list_folders")
    assert server_mode.logins == 1
    assert not server_mode.locks  # each call released its sync lock


def test_ping_connected_and_authenticated(server_mode):
    assert call_json("joplin_ping") == {
        "connected": True, "authenticated": True, "url": server_mode.url, "user": EMAIL,
        "message": "Joplin Server is accessible and authenticated"}
    assert server_mode.logins == 1


def test_ping_reports_a_bad_status(server_mode):
    server_mode.ping_status = 503
    assert call_json("joplin_ping") == {"connected": True, "authenticated": False,
                                        "error": "Server responded with status 503"}
    assert server_mode.logins == 0


def test_ping_without_credentials_reports_not_connected(server_mode, monkeypatch):
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_EMAIL", "")
    out = call_json("joplin_ping")
    assert out["connected"] is False and "must be set" in out["error"]


def test_ping_unreachable(server_mode, monkeypatch):
    server_mode.stop()
    out = call_json("joplin_ping")
    assert out["connected"] is False and set(out) == {"connected", "error"}


# ── notes ────────────────────────────────────────────────────────────────────

def test_create_note_writes_joplin_s_format(server_mode):
    out = call_json("joplin_create_note", title="Groceries", body="Crème brûlée ✓", parent_id=FOLDER)
    nid = out.pop("note_id")
    assert out == {"success": True, "title": "Groceries",
                   "message": "Note 'Groceries' created successfully"}
    text = server_mode.body(nid)
    assert text.startswith("Groceries\n\nCrème brûlée ✓\n\n")
    assert f"id: {nid}" in text and len(nid) == 32
    assert _meta(text) == ["type_: 1", f"parent_id: {FOLDER}", "updated_time: ", "is_todo: 0",
                           "source_application: joppy", "markup_language: 1"]
    assert call_json("joplin_get_note", note_id=nid, response_format="json")["body"] == "Crème brûlée ✓"


def test_create_todo_with_an_empty_body(server_mode):
    nid = call_json("joplin_create_note", title="Call", parent_id=FOLDER, is_todo=True)["note_id"]
    text = server_mode.body(nid)
    assert text.startswith("Call\n\n\n\ntype_: 1\n")
    assert "is_todo: 1" in _meta(text)


def test_create_note_reports_a_server_error(server_mode, monkeypatch):
    monkeypatch.setattr(uuid, "uuid4", lambda: type("U", (), {"hex": "e" * 32})())
    server_mode.fail[("PUT", "e" * 32 + ".md")] = 500
    out = call_json("joplin_create_note", title="X", parent_id=FOLDER)
    assert list(out) == ["error"] and "500" in out["error"]


def test_get_note_markdown(server_mode):
    nid = server_mode.add_note(1, "Plan", "line 1\nline 2")
    assert call("joplin_get_note", note_id=nid) == (
        "# Plan\n\n"
        f"**ID:** `{nid}`\n"
        "**Created:** 2026-01-02 03:04:05\n"
        "**Updated:** 2026-01-03 04:05:06\n"
        f"**Notebook ID:** `{FOLDER}`\n\n---\n\nline 1\nline 2")


def test_get_note_markdown_todo_states_and_bare_notes(server_mode):
    pending = server_mode.add_note(1, "Todo", "x", is_todo=1)
    done = server_mode.add_note(2, "Done", "x", is_todo=1, todo_completed=1767323045000)
    assert "**Todo Status:** Pending\n" in call("joplin_get_note", note_id=pending)
    assert "**Todo Status:** Completed\n" in call("joplin_get_note", note_id=done)
    bare = "b" * 32
    server_mode.put(f"{bare}.md", f"id: {bare}\ntype_: 1")
    assert call("joplin_get_note", note_id=bare) == (
        f"# Untitled\n\n**ID:** `{bare}`\n**Created:** N/A\n**Updated:** N/A\n\n---\n\n")


def test_get_note_json(server_mode):
    nid = server_mode.add_note(1, "Todo", "x", is_todo=1, todo_completed=1767323045000)
    assert call_json("joplin_get_note", note_id=nid, response_format="json") == {
        "id": nid, "parent_id": FOLDER, "title": "Todo", "body": "x",
        "created_time": "2026-01-02 03:04:05", "updated_time": "2026-01-03 04:05:06",
        "is_todo": True, "todo_due": "N/A", "todo_completed": "2026-01-02 03:04:05"}


def test_get_note_ignores_fields_joppy_does_not_know_and_nan_times(server_mode):
    nid = server_mode.add_note(1, "Plan", "x", extra=["share_key: zzz", "user_updated_time: NaN",
                                                      "user_created_time: nan"])
    assert call_json("joplin_get_note", note_id=nid, response_format="json")["title"] == "Plan"


def test_get_missing_note(server_mode):
    out = call_json("joplin_get_note", note_id="0" * 32)
    assert list(out) == ["error"] and "404" in out["error"]
    # As it is today: joppy's sync_lock skips its delete when the body raises, so the
    # lock stays on the server until it expires.
    assert len(server_mode.locks) == 1


def test_update_note_needs_a_field(server_mode):
    assert call_json("joplin_update_note", note_id="n1") == {"error": "No update fields specified"}
    assert server_mode.requests == []


def test_update_note_rewrites_only_what_it_is_given(server_mode):
    nid = server_mode.add_note(1, "Plan", "body", extra=["share_key: zzz", "user_updated_time: NaN"])
    assert call_json("joplin_update_note", note_id=nid, title="Plan B") == {
        "success": True, "note_id": nid, "message": "Note updated successfully"}
    text = server_mode.body(nid)
    assert text.startswith("Plan B\n\nbody\n\n")
    # Booleans go back as 0/1, times as the server stores them, unknown fields are dropped.
    assert _meta(text) == ["type_: 1", f"parent_id: {FOLDER}",
                           "created_time: 2026-01-02T03:04:05.678Z",
                           "updated_time: 2026-01-03T04:05:06.789Z", "is_conflict: 0", "is_todo: 0",
                           "source_application: net.example.desktop", "encryption_applied: 0",
                           "markup_language: 1", "is_shared: 0"]


def test_update_note_moves_and_keeps_a_true_flag(server_mode):
    nid = server_mode.add_note(1, "Plan", "body", is_todo=1)
    other = "1" * 32
    call_json("joplin_update_note", note_id=nid, parent_id=other, body="body 2")
    meta = _meta(server_mode.body(nid))
    assert f"parent_id: {other}" in meta and "is_todo: 1" in meta
    assert server_mode.body(nid).startswith("Plan\n\nbody 2\n\n")


def test_update_note_without_title_or_body(server_mode):
    bare = "b" * 32
    server_mode.put(f"{bare}.md", f"id: {bare}\nparent_id: {FOLDER}\ntype_: 1")
    other = "1" * 32
    call_json("joplin_update_note", note_id=bare, parent_id=other)
    assert server_mode.body(bare) == "\n".join([
        "", "", "type_: 1", f"id: {bare}", f"parent_id: {other}", "updated_time: ",
        "source_application: joppy", "markup_language: 1"])


def test_update_missing_note(server_mode):
    out = call_json("joplin_update_note", note_id="0" * 32, body="x")
    assert list(out) == ["error"] and "404" in out["error"]


@pytest.mark.parametrize("cmd", ["cat", "curl", "wget", "head", "tail", "echo", "printf"])
def test_update_refuses_an_unexpanded_shell_substitution(server_mode, cmd):
    nid = server_mode.add_note(1, "Plan", "old")
    body = f"intro $(  {cmd} /tmp/x.md) rest"
    out = call_json("joplin_update_note", note_id=nid, body=body)
    assert out == {
        "error": "body_rejected_shell_substitution",
        "message": (f"New body contains an unexpanded shell-substitution marker near the start "
                    f"('$(  {cmd}'). This is almost always a caller bug (e.g. an unexpanded "
                    "$(cat ...) heredoc). Pass force=true to override if this is intentional."),
        "matched_pattern": f"$(  {cmd}", "matched_pos": 6, "body_chars_new": len(body)}
    assert server_mode.writes() == []


def test_shell_substitution_guard_scope(server_mode):
    nid = server_mode.add_note(1, "Plan", "old")
    for body in ("$(catalog)", "$(ls x)", "x" * 252 + "$(cat y)"):
        assert "success" in call_json("joplin_update_note", note_id=nid, body=body), body
    # Only the first 256 characters are scanned.
    edge = "x" * 251 + "$(cat y)"
    assert call_json("joplin_update_note", note_id=nid, body=edge)["matched_pos"] == 251
    assert "success" in call_json("joplin_update_note", note_id=nid, body="$(cat y)", force=True)


def test_update_refuses_a_destructive_shrink(server_mode):
    nid = server_mode.add_note(1, "Plan", "y" * 1000)
    out = call_json("joplin_update_note", note_id=nid, body="y" * 499)
    assert out == {
        "error": "body_rejected_shrink_guard",
        "message": ("New body (499 chars) is less than 50% of the current body (1000 chars). "
                    "Refusing to commit a destructive shrink. Pass force=true if this is "
                    "intentional (e.g. migration / drastic restructure)."),
        "body_chars_new": 499, "body_chars_current": 1000, "shrink_threshold_pct": 50}
    assert server_mode.writes() == []
    assert "success" in call_json("joplin_update_note", note_id=nid, body="y" * 500)


def test_shrink_guard_spares_small_notes_and_force(server_mode):
    small = server_mode.add_note(1, "Plan", "y" * 999)
    assert "success" in call_json("joplin_update_note", note_id=small, body="")
    big = server_mode.add_note(2, "Plan", "y" * 2000)
    assert "success" in call_json("joplin_update_note", note_id=big, body="z", force=True)
    assert server_mode.body(big).startswith("Plan\n\nz\n\n")


def test_shrink_guard_on_a_note_without_body(server_mode):
    nid = server_mode.add_note(1, "Plan", None)
    assert "success" in call_json("joplin_update_note", note_id=nid, body="new")


def test_shrink_guard_on_an_item_that_is_not_a_note(server_mode):
    rid = "c" * 32
    server_mode.put(f"{rid}.md", f"id: {rid}\nitem_id: {'d' * 32}\ntype_: 13")
    out = call_json("joplin_update_note", note_id=rid, body="x")
    assert list(out) == ["error"]  # modify_note cannot set a body on a revision
    assert server_mode.writes() == []


def test_delete_note(server_mode):
    nid = server_mode.add_note(1, "Plan", "x")
    assert call_json("joplin_delete_note", note_id=nid) == {
        "success": True, "message": "Note deleted successfully"}
    assert f"{nid}.md" not in server_mode.items
    assert "404" in call_json("joplin_delete_note", note_id=nid)["error"]


def _seed_list(fake):
    a = fake.add_note(1, "Alpha", "first body", is_todo=1)
    b = fake.add_note(2, "Beta", "Second BODY", is_todo=1, todo_completed=1767323045000)
    c = fake.add_note(3, "", "gamma body", parent_id="1" * 32)
    fake.put("d" * 32 + ".md", folder_text("d" * 32, "A folder"))
    return a, b, c


def test_list_notes_markdown(server_mode):
    a, b, c = _seed_list(server_mode)
    assert call("joplin_list_notes") == "\n".join([
        "# Notes", "",
        "- [ ] **Alpha**", f"  - ID: `{a}`", "  - Updated: 2026-01-03 04:05:06",
        "- [x] **Beta**", f"  - ID: `{b}`", "  - Updated: 2026-01-03 04:05:06",
        "- **Untitled**", f"  - ID: `{c}`", "  - Updated: 2026-01-03 04:05:06",
        "\n*Total: 3 notes*"])


def test_list_notes_in_a_folder_as_json(server_mode):
    a, b, c = _seed_list(server_mode)
    out = call_json("joplin_list_notes", folder_id="1" * 32, response_format="json")
    assert out["count"] == 1 and [n["id"] for n in out["items"]] == [c]
    assert out["items"][0]["body"] == "gamma body"
    out = call_json("joplin_list_notes", folder_id="", response_format="json")
    assert out["count"] == 3


def test_list_notes_empty(server_mode):
    assert call("joplin_list_notes", folder_id="9" * 32) == "# Notes\n\n*No notes found*\n\n*Total: 0 notes*"


def test_list_notes_error(server_mode, monkeypatch):
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_EMAIL", "")
    assert "must be set" in call_json("joplin_list_notes")["error"]


def test_search_notes_markdown(server_mode):
    a, b, c = _seed_list(server_mode)
    assert call("joplin_search_notes", query="BODY") == "\n".join([
        "# Search Results for 'BODY'", "",
        "- **Alpha**", f"  - ID: `{a}`", "- **Beta**", f"  - ID: `{b}`",
        "- **Untitled**", f"  - ID: `{c}`", "\n*Found 3 matching notes*"])


def test_search_matches_title_or_body_case_insensitively(server_mode):
    a, b, c = _seed_list(server_mode)
    server_mode.add_note(4, "Delta", None)
    out = call_json("joplin_search_notes", query="alph", response_format="json")
    assert out["query"] == "alph" and out["count"] == 1 and out["items"][0]["id"] == a
    assert [n["id"] for n in call_json("joplin_search_notes", query="second", response_format="json")["items"]] == [b]
    assert call_json("joplin_search_notes", query="delta", response_format="json")["count"] == 1
    assert call("joplin_search_notes", query="nowhere") == (
        "# Search Results for 'nowhere'\n\n*No results found*\n\n*Found 0 matching notes*")


def test_search_error(server_mode, monkeypatch):
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_EMAIL", "")
    assert "must be set" in call_json("joplin_search_notes", query="x")["error"]


# ── notebooks and tags ───────────────────────────────────────────────────────

def test_create_folder(server_mode):
    out = call_json("joplin_create_folder", title="Projects")
    fid = out.pop("folder_id")
    assert out == {"success": True, "title": "Projects", "message": "Notebook 'Projects' created successfully"}
    assert "parent_id" not in server_mode.items[f"{fid}.md"]
    sub = call_json("joplin_create_folder", title="Sub", parent_id=fid)["folder_id"]
    assert f"parent_id: {fid}" in server_mode.items[f"{sub}.md"]
    assert "404" not in call("joplin_create_folder", title="Top", parent_id="")


def test_create_folder_error(server_mode, monkeypatch):
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_EMAIL", "")
    assert "must be set" in call_json("joplin_create_folder", title="x")["error"]


def test_list_folders(server_mode):
    assert call("joplin_list_folders") == "# Notebooks\n\n*No notebooks found*"
    server_mode.put("d" * 32 + ".md", folder_text("d" * 32, "Projects", parent_id="e" * 32))
    server_mode.put("e" * 32 + ".md", f"id: {'e' * 32}\ntype_: 2")
    server_mode.add_note(1, "A note", "x")
    assert call("joplin_list_folders") == "\n".join([
        "# Notebooks", "", "- **Projects**", f"  - ID: `{'d' * 32}`",
        "- **Untitled**", f"  - ID: `{'e' * 32}`"])
    assert call_json("joplin_list_folders", response_format="json") == {"folders": [
        {"id": "d" * 32, "title": "Projects", "parent_id": "e" * 32,
         "created_time": "2026-01-02 03:04:05", "updated_time": "2026-01-02 03:04:05"},
        {"id": "e" * 32, "title": None, "parent_id": None, "created_time": "N/A", "updated_time": "N/A"}]}


def test_list_folders_error(server_mode, monkeypatch):
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_EMAIL", "")
    assert "must be set" in call_json("joplin_list_folders")["error"]


def test_delete_folder(server_mode):
    server_mode.put("d" * 32 + ".md", folder_text("d" * 32, "Projects"))
    assert call_json("joplin_delete_folder", folder_id="d" * 32) == {
        "success": True, "message": "Notebook deleted successfully"}
    assert "404" in call_json("joplin_delete_folder", folder_id="d" * 32)["error"]


def test_tags(server_mode):
    assert call("joplin_list_tags") == "# Tags\n\n*No tags found*"
    out = call_json("joplin_create_tag", title="urgent")
    tid = out.pop("tag_id")
    assert out == {"success": True, "title": "urgent", "message": "Tag 'urgent' created successfully"}
    server_mode.put("a" * 32 + ".md", f"id: {'a' * 32}\ntype_: 5")
    head, _, lines = call("joplin_list_tags").partition("\n\n")
    assert head == "# Tags"
    assert sorted(lines.split("\n")) == [f"- **Untitled** (`{'a' * 32}`)", f"- **urgent** (`{tid}`)"]
    tags = {t["id"]: t for t in call_json("joplin_list_tags", response_format="json")["tags"]}
    assert tags[tid] == {"id": tid, "title": "urgent", "created_time": "N/A", "updated_time": "N/A"}
    server_mode.put("b" * 32 + ".md", tag_text("b" * 32, "later"))
    tags = {t["id"]: t for t in call_json("joplin_list_tags", response_format="json")["tags"]}
    assert tags["b" * 32]["created_time"] == "2026-01-02 03:04:05"


def test_tag_errors(server_mode, monkeypatch):
    monkeypatch.setattr(jsm, "JOPLIN_SERVER_EMAIL", "")
    assert "must be set" in call_json("joplin_list_tags")["error"]
    assert "must be set" in call_json("joplin_create_tag", title="x")["error"]
    assert "must be set" in call_json("joplin_add_tag_to_note", tag_id="t", note_id="n")["error"]


def test_add_tag_to_note(server_mode):
    nid = server_mode.add_note(1, "Plan", "x")
    tid = "a" * 32
    assert call_json("joplin_add_tag_to_note", tag_id=tid, note_id=nid) == {
        "success": True, "message": "Tag added to note successfully"}
    link = [t for n, t in server_mode.items.items() if "type_: 6" in t]
    assert len(link) == 1 and f"note_id: {nid}" in link[0] and f"tag_id: {tid}" in link[0]


# ── section and patch edits ──────────────────────────────────────────────────

DOC = "\n".join([
    "# Backlog", "", "intro", "",
    "## Doing", "", "- one", "### Detail", "- deep", "", "",
    "## Done", "", "- old",
    "# Archive", "- gone",
])


def _edit(fake, tool, body=DOC, **params):
    nid = fake.add_note(1, "Backlog", body)
    out = call_json(tool, note_id=nid, **params)
    return out, fake.body(nid).rpartition("\n\n")[0].split("\n\n", 1)[1], nid


def test_append_bottom_trims_blank_lines_and_keeps_subsections(server_mode):
    out, body, nid = _edit(server_mode, "joplin_append_to_section", section_heading="Doing", content="- two")
    assert body == DOC.replace("- deep\n\n\n## Done", "- deep\n\n- two\n\n## Done")
    assert out == {"success": True, "note_id": nid, "section_heading": "Doing", "position": "bottom",
                   "appended_chars": 5, "body_chars_before": len(DOC), "body_chars_after": len(body),
                   "message": "Section appended successfully"}


def test_append_top_after_the_blank_line(server_mode):
    _, body, _ = _edit(server_mode, "joplin_append_to_section", section_heading="  Doing ",
                       content="- zero\n", position="top")
    assert body == DOC.replace("## Doing\n\n- one", "## Doing\n\n- zero\n\n- one")


def test_append_top_without_a_blank_line_and_multiline(server_mode):
    _, body, _ = _edit(server_mode, "joplin_append_to_section", section_heading="Done",
                       content="- a\n- b\n\n", position="top")
    assert body == DOC.replace("## Done\n\n- old", "## Done\n\n- a\n- b\n\n- old")
    _, body, _ = _edit(server_mode, "joplin_append_to_section", body="## Done\n- old",
                       section_heading="Done", content="- a", position="top")
    assert body == "## Done\n- a\n\n- old"


def test_append_to_a_heading_at_the_end(server_mode):
    _, body, _ = _edit(server_mode, "joplin_append_to_section", body="x\n## Done",
                       section_heading="Done", content="- a", position="top")
    assert body == "x\n## Done\n- a\n"
    _, body, _ = _edit(server_mode, "joplin_append_to_section", body="x\n## Done\n\n",
                       section_heading="Done", content="- a")
    assert body == "x\n## Done\n\n- a\n"


def test_append_to_the_last_section_keeps_the_trailing_newline(server_mode):
    _, body, _ = _edit(server_mode, "joplin_append_to_section", body="## A\n- x\n",
                       section_heading="A", content="- y")
    assert body == "## A\n- x\n\n- y\n"
    _, body, _ = _edit(server_mode, "joplin_append_to_section", body="## A\n- x",
                       section_heading="A", content="- y")
    assert body == "## A\n- x\n\n- y\n"
    _, body, _ = _edit(server_mode, "joplin_append_to_section", body="## A\n- x\n## B\n- z\n",
                       section_heading="A", content="- y")
    assert body == "## A\n- x\n\n- y\n\n## B\n- z\n"
    _, body, _ = _edit(server_mode, "joplin_append_to_section", body="## A\n- x\n## B\n- z",
                       section_heading="A", content="- y")
    assert body == "## A\n- x\n\n- y\n\n## B\n- z"


def test_indented_lines_stay_inside_their_section(server_mode):
    body = "## A\n- x\n  - nested\n    code\n## B\n- z"
    _, out, _ = _edit(server_mode, "joplin_append_to_section", body=body, section_heading="A", content="- y")
    assert out == "## A\n- x\n  - nested\n    code\n\n- y\n\n## B\n- z"
    _, out, _ = _edit(server_mode, "joplin_replace_section", body=body, section_heading="A", new_content="- y")
    assert out == "## A\n\n- y\n\n## B\n- z"


def test_append_level_one_section_runs_to_the_next_level_one(server_mode):
    _, body, _ = _edit(server_mode, "joplin_append_to_section", section_heading="Backlog",
                       heading_level=1, content="- end")
    assert body == DOC.replace("- old\n# Archive", "- old\n\n- end\n\n# Archive")


@pytest.mark.parametrize("tool,extra", [("joplin_append_to_section", {"content": "x"}),
                                        ("joplin_replace_section", {"new_content": "x"})])
def test_section_not_found_and_ambiguous(server_mode, tool, extra):
    nid = server_mode.add_note(1, "Backlog", "## A\n- 1\n## A\n- 2\n### B\n")
    assert call(tool, note_id=nid, section_heading="Zed", **extra) == json.dumps(
        {"error": "section_not_found", "details": {"heading": "Zed", "heading_level": 2}}, indent=2)
    assert call(tool, note_id=nid, section_heading="B", **extra) == json.dumps(
        {"error": "section_not_found", "details": {"heading": "B", "heading_level": 2}}, indent=2)
    assert call(tool, note_id=nid, section_heading="A", **extra) == json.dumps(
        {"error": "section_ambiguous", "details": {"heading": "A", "heading_level": 2,
                                                   "match_lines": [1, 3]}}, indent=2)
    assert server_mode.writes() == []


@pytest.mark.parametrize("tool,extra", [("joplin_append_to_section", {"content": "x"}),
                                        ("joplin_replace_section", {"new_content": "x"}),
                                        ("joplin_apply_patch", {"operations": [{"search": "a"}]})])
def test_edits_on_a_missing_note_and_a_note_without_body(server_mode, tool, extra):
    out = call_json(tool, note_id="0" * 32, **({"section_heading": "A"} if "section" in tool else {}), **extra)
    assert list(out) == ["error"] and "404" in out["error"]
    nid = server_mode.add_note(1, "Empty", None)
    out = call_json(tool, note_id=nid, **({"section_heading": "A"} if "section" in tool else {}), **extra)
    assert out["error"] in ("section_not_found", "search_not_found")


def test_replace_section_in_the_middle(server_mode):
    out, body, nid = _edit(server_mode, "joplin_replace_section", section_heading="Doing",
                           new_content="- new\n- newer\n")
    assert body == DOC.replace("## Doing\n\n- one\n### Detail\n- deep\n\n\n## Done",
                               "## Doing\n\n- new\n- newer\n\n## Done")
    assert out == {"success": True, "note_id": nid, "section_heading": "Doing",
                   "body_chars_before": len(DOC), "body_chars_after": len(body),
                   "message": "Section replaced successfully"}


def test_replace_last_section_and_empty_content(server_mode):
    _, body, _ = _edit(server_mode, "joplin_replace_section", body="## A\n- x\n## B\n- y\n",
                       section_heading="B", new_content="- z")
    assert body == "## A\n- x\n## B\n\n- z\n"
    _, body, _ = _edit(server_mode, "joplin_replace_section", body="## A\n- x\n## B\n- y",
                       section_heading="A", new_content="")
    assert body == "## A\n\n\n## B\n- y"
    _, body, _ = _edit(server_mode, "joplin_replace_section", body="## A\n- x",
                       section_heading="A", new_content="- y")
    assert body == "## A\n\n- y"


def test_apply_patch_in_order(server_mode):
    out, body, nid = _edit(server_mode, "joplin_apply_patch", body="a b c",
                           operations=[{"search": "b", "replace": "bb"}, {"search": "bb c", "replace": ""},
                                       {"search": "a"}])
    assert body == " "
    assert out == {"success": True, "note_id": nid, "operations_applied": 3, "body_chars_before": 5,
                   "body_chars_after": 1, "message": "Patch applied successfully"}


def test_apply_patch_refuses_missing_or_repeated_text_and_writes_nothing(server_mode):
    nid = server_mode.add_note(1, "P", "x y x")
    long = "q" * 100
    assert call_json("joplin_apply_patch", note_id=nid, operations=[{"search": "y", "replace": "Y"},
                                                                    {"search": long}]) == {
        "error": "search_not_found", "details": {"op_index": 1, "search_preview": "q" * 80, "search_len": 100}}
    assert call_json("joplin_apply_patch", note_id=nid, operations=[{"search": "x"}]) == {
        "error": "search_not_unique", "details": {"op_index": 0, "search_preview": "x", "match_count": 2}}
    assert server_mode.writes() == []
