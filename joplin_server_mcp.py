"""
Joplin Server MCP - MCP server for Joplin Server (self-hosted sync server).

This server uses the joppy library to interact with Joplin Server's API.
Unlike the Desktop Web Clipper API, this connects directly to Joplin Server.

Requirements:
    - Joplin Server running (e.g., via Docker)
    - Valid user credentials (email/password)
    - joppy library installed

Environment variables:
    - JOPLIN_SERVER_URL: Server URL (default: http://localhost:22300)
    - JOPLIN_SERVER_EMAIL: User email
    - JOPLIN_SERVER_PASSWORD: User password
"""

import json
import os
import sys
from enum import Enum
from typing import Optional, List, Any
from datetime import datetime
from contextlib import contextmanager

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict, model_validator

from joppy.server_api import ServerApi, LockError

# --- patch: pin joppy responses to utf-8 -------------------------------------
# Joplin Server returns note content as text/plain without a charset, so
# requests leaves Response.encoding unset and joppy's '.text' mis-decodes the
# UTF-8 body. A response hook forces utf-8 so note bodies round-trip cleanly.
import joppy.server_api as _joppy_server_api

def _joppy_force_utf8(_resp, *_args, **_kwargs):
    _resp.encoding = 'utf-8'
    return _resp

_joppy_server_api.SESSION.hooks.setdefault('response', [])
if _joppy_force_utf8 not in _joppy_server_api.SESSION.hooks['response']:
    _joppy_server_api.SESSION.hooks['response'].append(_joppy_force_utf8)
# -----------------------------------------------------------------------------

from joppy import data_types as dt

# Monkey-patch joppy to handle newer Joplin Server fields, NaN timestamps,
# and fix boolean serialization (Joplin expects 0/1, not False/True)

def _filter_kwargs(dataclass_type, kwargs):
    """Filter kwargs to only include known fields and fix NaN timestamps."""
    known_fields = {f.name for f in dt.fields(dataclass_type)}
    filtered = {}
    for k, v in kwargs.items():
        if k not in known_fields:
            continue
        # Handle NaN timestamp values
        if v == "NaN" or v == "nan":
            v = None
        filtered[k] = v
    return filtered

_original_resource_init = dt.ResourceData.__init__
def _patched_resource_init(self, **kwargs):
    _original_resource_init(self, **_filter_kwargs(dt.ResourceData, kwargs))
dt.ResourceData.__init__ = _patched_resource_init

_original_note_init = dt.NoteData.__init__
def _patched_note_init(self, **kwargs):
    _original_note_init(self, **_filter_kwargs(dt.NoteData, kwargs))
dt.NoteData.__init__ = _patched_note_init

_original_notebook_init = dt.NotebookData.__init__
def _patched_notebook_init(self, **kwargs):
    _original_notebook_init(self, **_filter_kwargs(dt.NotebookData, kwargs))
dt.NotebookData.__init__ = _patched_notebook_init

_original_tag_init = dt.TagData.__init__
def _patched_tag_init(self, **kwargs):
    _original_tag_init(self, **_filter_kwargs(dt.TagData, kwargs))
dt.TagData.__init__ = _patched_tag_init

# Fix NoteData serialization to output 0/1 for boolean fields instead of False/True
_original_note_serialize = dt.NoteData.serialize
def _patched_note_serialize(self):
    """Patched serialize that converts booleans to 0/1."""
    from dataclasses import fields as dc_fields

    # Boolean fields that need 0/1 instead of False/True
    bool_fields = {'is_todo', 'is_conflict', 'encryption_applied', 'is_shared'}

    lines = ["" if self.title is None else self.title, ""]
    if self.body is not None:
        lines.extend([self.body, ""])

    for field_ in dc_fields(self):
        if field_.name == "id":
            if self.id is None:
                import uuid
                self.id = uuid.uuid4().hex
            lines.append(f"{field_.name}: {self.id}")
        elif field_.name == "markup_language":
            if self.markup_language is None:
                self.markup_language = dt.MarkupLanguage.MARKDOWN
            lines.append(f"{field_.name}: {int(self.markup_language)}")
        elif field_.name == "source_application":
            if self.source_application is None:
                self.source_application = "joppy"
            lines.append(f"{field_.name}: {self.source_application}")
        elif field_.name in ("title", "body"):
            pass  # handled before
        elif field_.name == "type_":
            lines.append(f"{field_.name}: {int(dt.ItemType.NOTE)}")
        elif field_.name == "updated_time":
            value_raw = getattr(self, field_.name)
            value = "" if value_raw is None else value_raw
            lines.append(f"{field_.name}: {value}")
        elif field_.name in bool_fields:
            value_raw = getattr(self, field_.name)
            if value_raw is not None:
                # Convert bool to 0/1
                lines.append(f"{field_.name}: {1 if value_raw else 0}")
        else:
            value_raw = getattr(self, field_.name)
            if value_raw is not None:
                lines.append(f"{field_.name}: {value_raw}")
    return "\n".join(lines)

dt.NoteData.serialize = _patched_note_serialize

# =============================================================================
# Configuration
# =============================================================================

JOPLIN_SERVER_URL = os.getenv("JOPLIN_SERVER_URL", "http://localhost:22300")
JOPLIN_SERVER_EMAIL = os.getenv("JOPLIN_SERVER_EMAIL", "")
JOPLIN_SERVER_PASSWORD = os.getenv("JOPLIN_SERVER_PASSWORD", "")

# Global API instance (lazy initialized)
_api: Optional[ServerApi] = None

# =============================================================================
# Initialize MCP Server
# =============================================================================

mcp = FastMCP("joplin_server_mcp")

# =============================================================================
# API Connection Helper
# =============================================================================

def _get_api() -> ServerApi:
    """Get or create the Joplin Server API connection."""
    global _api
    if _api is None:
        if not JOPLIN_SERVER_EMAIL or not JOPLIN_SERVER_PASSWORD:
            raise ValueError(
                "JOPLIN_SERVER_EMAIL and JOPLIN_SERVER_PASSWORD must be set. "
                "These are your Joplin Server login credentials."
            )
        _api = ServerApi(
            user=JOPLIN_SERVER_EMAIL,
            password=JOPLIN_SERVER_PASSWORD,
            url=JOPLIN_SERVER_URL
        )
    return _api


@contextmanager
def _with_lock():
    """Context manager to acquire sync lock for operations."""
    api = _get_api()
    with api.sync_lock():
        yield api


def _format_timestamp(dt_obj: Optional[datetime]) -> str:
    """Format datetime to readable string."""
    if not dt_obj:
        return "N/A"
    return dt_obj.strftime("%Y-%m-%d %H:%M:%S")


def _note_to_dict(note: dt.NoteData) -> dict:
    """Convert NoteData to dictionary."""
    return {
        "id": note.id,
        "parent_id": note.parent_id,
        "title": note.title,
        "body": note.body,
        "created_time": _format_timestamp(note.created_time),
        "updated_time": _format_timestamp(note.updated_time),
        "is_todo": note.is_todo,
        "todo_due": _format_timestamp(note.todo_due),
        "todo_completed": _format_timestamp(note.todo_completed),
    }


def _notebook_to_dict(notebook: dt.NotebookData) -> dict:
    """Convert NotebookData to dictionary."""
    return {
        "id": notebook.id,
        "title": notebook.title,
        "parent_id": notebook.parent_id,
        "created_time": _format_timestamp(notebook.created_time),
        "updated_time": _format_timestamp(notebook.updated_time),
    }


def _tag_to_dict(tag: dt.TagData) -> dict:
    """Convert TagData to dictionary."""
    return {
        "id": tag.id,
        "title": tag.title,
        "created_time": _format_timestamp(tag.created_time),
        "updated_time": _format_timestamp(tag.updated_time),
    }


# =============================================================================
# Enums and Input Models
# =============================================================================

class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class CreateNoteInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="The note title", min_length=1)
    body: str = Field(default="", description="The note body in Markdown format")
    parent_id: str = Field(..., description="ID of the notebook to create the note in (required)")
    is_todo: Optional[bool] = Field(default=False, description="Whether this note is a todo item")


class UpdateNoteInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note_id: str = Field(..., description="The ID of the note to update")
    title: Optional[str] = Field(default=None, description="New title for the note")
    body: Optional[str] = Field(default=None, description="New body content in Markdown")
    parent_id: Optional[str] = Field(default=None, description="Move note to this notebook ID")


class GetNoteInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note_id: str = Field(..., description="The ID of the note to retrieve")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class DeleteNoteInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note_id: str = Field(..., description="The ID of the note to delete")


class ListNotesInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(extra="forbid")

    folder_id: Optional[str] = Field(default=None, description="Filter notes by notebook ID")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class SearchNotesInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Search query (searches in title and body)", min_length=1)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class CreateFolderInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="The notebook title", min_length=1)
    parent_id: Optional[str] = Field(default=None, description="Parent notebook ID")


class ListFoldersInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(extra="forbid")

    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class DeleteFolderInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    folder_id: str = Field(..., description="The ID of the notebook to delete")


class CreateTagInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="The tag name", min_length=1)


class ListTagsInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(extra="forbid")

    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class AddTagToNoteInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                # supergateway may append a trailing '}' from the outer wrapper;
                # strip it and retry
                if 'Extra data' in str(e) and v.endswith('}'):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    tag_id: str = Field(..., description="The tag ID")
    note_id: str = Field(..., description="The note ID to tag")


# =============================================================================
# Note Tools
# =============================================================================

@mcp.tool(
    name="joplin_create_note",
    annotations={
        "title": "Create a Joplin Note",
        "readOnlyHint": False,
        "destructiveHint": False,
    }
)
def joplin_create_note(params: CreateNoteInput) -> str:
    """Create a new note in Joplin Server.

    Args:
        params: CreateNoteInput with title, body, parent_id (required), is_todo

    Returns:
        JSON with created note ID and details
    """
    try:
        with _with_lock() as api:
            # Convert boolean to int for Joplin compatibility
            note_id = api.add_note(
                parent_id=params.parent_id,
                title=params.title,
                body=params.body,
                is_todo=1 if params.is_todo else 0,
            )
            return json.dumps({
                "success": True,
                "note_id": note_id,
                "title": params.title,
                "message": f"Note '{params.title}' created successfully"
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_get_note",
    annotations={
        "title": "Get a Joplin Note",
        "readOnlyHint": True,
    }
)
def joplin_get_note(params: GetNoteInput) -> str:
    """Retrieve a specific note by ID.

    Args:
        params: GetNoteInput with note_id and response_format

    Returns:
        Note content in requested format
    """
    try:
        with _with_lock() as api:
            note = api.get_note(params.note_id)

            if params.response_format == ResponseFormat.JSON:
                return json.dumps(_note_to_dict(note), indent=2)

            # Markdown format
            lines = [
                f"# {note.title or 'Untitled'}",
                "",
                f"**ID:** `{note.id}`",
                f"**Created:** {_format_timestamp(note.created_time)}",
                f"**Updated:** {_format_timestamp(note.updated_time)}",
            ]

            if note.is_todo:
                status = "Completed" if note.todo_completed else "Pending"
                lines.append(f"**Todo Status:** {status}")

            if note.parent_id:
                lines.append(f"**Notebook ID:** `{note.parent_id}`")

            lines.extend(["", "---", "", note.body or ""])
            return "\n".join(lines)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_update_note",
    annotations={
        "title": "Update a Joplin Note",
        "readOnlyHint": False,
    }
)
def joplin_update_note(params: UpdateNoteInput) -> str:
    """Update an existing note.

    Args:
        params: UpdateNoteInput with note_id and fields to update

    Returns:
        JSON confirmation or error
    """
    try:
        update_data = {}
        if params.title is not None:
            update_data["title"] = params.title
        if params.body is not None:
            update_data["body"] = params.body
        if params.parent_id is not None:
            update_data["parent_id"] = params.parent_id

        if not update_data:
            return json.dumps({"error": "No update fields specified"}, indent=2)

        with _with_lock() as api:
            api.modify_note(params.note_id, **update_data)
            return json.dumps({
                "success": True,
                "note_id": params.note_id,
                "message": "Note updated successfully"
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_delete_note",
    annotations={
        "title": "Delete a Joplin Note",
        "readOnlyHint": False,
        "destructiveHint": True,
    }
)
def joplin_delete_note(params: DeleteNoteInput) -> str:
    """Delete a note from Joplin Server.

    Args:
        params: DeleteNoteInput with note_id

    Returns:
        JSON confirmation or error
    """
    try:
        with _with_lock() as api:
            api.delete_note(params.note_id)
            return json.dumps({
                "success": True,
                "message": "Note deleted successfully"
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_list_notes",
    annotations={
        "title": "List Joplin Notes",
        "readOnlyHint": True,
    }
)
def joplin_list_notes(params: ListNotesInput) -> str:
    """List all notes, optionally filtered by notebook.

    Args:
        params: ListNotesInput with optional folder_id and response_format

    Returns:
        List of notes in requested format
    """
    try:
        with _with_lock() as api:
            all_notes = api.get_all_notes()

            # Filter by folder if specified
            if params.folder_id:
                all_notes = [n for n in all_notes if n.parent_id == params.folder_id]

            if params.response_format == ResponseFormat.JSON:
                return json.dumps({
                    "items": [_note_to_dict(n) for n in all_notes],
                    "count": len(all_notes)
                }, indent=2)

            # Markdown format
            lines = ["# Notes", ""]

            if not all_notes:
                lines.append("*No notes found*")
            else:
                for note in all_notes:
                    prefix = ""
                    if note.is_todo:
                        prefix = "[x] " if note.todo_completed else "[ ] "
                    lines.append(f"- {prefix}**{note.title or 'Untitled'}**")
                    lines.append(f"  - ID: `{note.id}`")
                    lines.append(f"  - Updated: {_format_timestamp(note.updated_time)}")

            lines.append(f"\n*Total: {len(all_notes)} notes*")
            return "\n".join(lines)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_search_notes",
    annotations={
        "title": "Search Joplin Notes",
        "readOnlyHint": True,
    }
)
def joplin_search_notes(params: SearchNotesInput) -> str:
    """Search notes by title or body content.

    Note: This performs client-side filtering as Joplin Server doesn't have
    a native search API.

    Args:
        params: SearchNotesInput with query and response_format

    Returns:
        Matching notes in requested format
    """
    try:
        with _with_lock() as api:
            all_notes = api.get_all_notes()
            query_lower = params.query.lower()

            # Client-side search
            matches = []
            for note in all_notes:
                title_match = note.title and query_lower in note.title.lower()
                body_match = note.body and query_lower in note.body.lower()
                if title_match or body_match:
                    matches.append(note)

            if params.response_format == ResponseFormat.JSON:
                return json.dumps({
                    "query": params.query,
                    "items": [_note_to_dict(n) for n in matches],
                    "count": len(matches)
                }, indent=2)

            # Markdown format
            lines = [f"# Search Results for '{params.query}'", ""]

            if not matches:
                lines.append("*No results found*")
            else:
                for note in matches:
                    lines.append(f"- **{note.title or 'Untitled'}**")
                    lines.append(f"  - ID: `{note.id}`")

            lines.append(f"\n*Found {len(matches)} matching notes*")
            return "\n".join(lines)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


# =============================================================================
# Folder/Notebook Tools
# =============================================================================

@mcp.tool(
    name="joplin_create_folder",
    annotations={
        "title": "Create a Joplin Notebook",
        "readOnlyHint": False,
    }
)
def joplin_create_folder(params: CreateFolderInput) -> str:
    """Create a new notebook.

    Args:
        params: CreateFolderInput with title and optional parent_id

    Returns:
        JSON with created notebook details
    """
    try:
        with _with_lock() as api:
            kwargs = {"title": params.title}
            if params.parent_id:
                kwargs["parent_id"] = params.parent_id

            folder_id = api.add_notebook(**kwargs)
            return json.dumps({
                "success": True,
                "folder_id": folder_id,
                "title": params.title,
                "message": f"Notebook '{params.title}' created successfully"
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_list_folders",
    annotations={
        "title": "List Joplin Notebooks",
        "readOnlyHint": True,
    }
)
def joplin_list_folders(params: ListFoldersInput) -> str:
    """List all notebooks.

    Args:
        params: ListFoldersInput with response_format

    Returns:
        List of notebooks in requested format
    """
    try:
        with _with_lock() as api:
            all_folders = api.get_all_notebooks()

            if params.response_format == ResponseFormat.JSON:
                return json.dumps({
                    "folders": [_notebook_to_dict(f) for f in all_folders]
                }, indent=2)

            # Markdown format
            lines = ["# Notebooks", ""]

            if not all_folders:
                lines.append("*No notebooks found*")
            else:
                for folder in all_folders:
                    lines.append(f"- **{folder.title or 'Untitled'}**")
                    lines.append(f"  - ID: `{folder.id}`")

            return "\n".join(lines)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_delete_folder",
    annotations={
        "title": "Delete a Joplin Notebook",
        "readOnlyHint": False,
        "destructiveHint": True,
    }
)
def joplin_delete_folder(params: DeleteFolderInput) -> str:
    """Delete a notebook.

    Args:
        params: DeleteFolderInput with folder_id

    Returns:
        JSON confirmation or error
    """
    try:
        with _with_lock() as api:
            api.delete_notebook(params.folder_id)
            return json.dumps({
                "success": True,
                "message": "Notebook deleted successfully"
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


# =============================================================================
# Tag Tools
# =============================================================================

@mcp.tool(
    name="joplin_create_tag",
    annotations={
        "title": "Create a Joplin Tag",
        "readOnlyHint": False,
    }
)
def joplin_create_tag(params: CreateTagInput) -> str:
    """Create a new tag.

    Args:
        params: CreateTagInput with title

    Returns:
        JSON with created tag details
    """
    try:
        with _with_lock() as api:
            tag_id = api.add_tag(title=params.title)
            return json.dumps({
                "success": True,
                "tag_id": tag_id,
                "title": params.title,
                "message": f"Tag '{params.title}' created successfully"
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_list_tags",
    annotations={
        "title": "List Joplin Tags",
        "readOnlyHint": True,
    }
)
def joplin_list_tags(params: ListTagsInput) -> str:
    """List all tags.

    Args:
        params: ListTagsInput with response_format

    Returns:
        List of tags in requested format
    """
    try:
        with _with_lock() as api:
            all_tags = api.get_all_tags()

            if params.response_format == ResponseFormat.JSON:
                return json.dumps({
                    "tags": [_tag_to_dict(t) for t in all_tags]
                }, indent=2)

            # Markdown format
            lines = ["# Tags", ""]

            if not all_tags:
                lines.append("*No tags found*")
            else:
                for tag in all_tags:
                    lines.append(f"- **{tag.title or 'Untitled'}** (`{tag.id}`)")

            return "\n".join(lines)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_add_tag_to_note",
    annotations={
        "title": "Add Tag to Note",
        "readOnlyHint": False,
    }
)
def joplin_add_tag_to_note(params: AddTagToNoteInput) -> str:
    """Add a tag to a note.

    Args:
        params: AddTagToNoteInput with tag_id and note_id

    Returns:
        JSON confirmation or error
    """
    try:
        with _with_lock() as api:
            api.add_tag_to_note(tag_id=params.tag_id, note_id=params.note_id)
            return json.dumps({
                "success": True,
                "message": "Tag added to note successfully"
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


# =============================================================================
# Utility Tools
# =============================================================================

@mcp.tool(
    name="joplin_ping",
    annotations={
        "title": "Check Joplin Server Connection",
        "readOnlyHint": True,
    }
)
def joplin_ping() -> str:
    """Check if Joplin Server is accessible.

    Returns:
        JSON with connection status
    """
    import requests
    try:
        # Direct ping without going through joppy (avoids lock requirement)
        response = requests.get(f"{JOPLIN_SERVER_URL}/api/ping", timeout=10)
        if response.status_code == 200:
            # Also verify we can authenticate
            api = _get_api()
            return json.dumps({
                "connected": True,
                "authenticated": True,
                "url": JOPLIN_SERVER_URL,
                "user": JOPLIN_SERVER_EMAIL,
                "message": "Joplin Server is accessible and authenticated"
            }, indent=2)
        else:
            return json.dumps({
                "connected": True,
                "authenticated": False,
                "error": f"Server responded with status {response.status_code}"
            }, indent=2)
    except Exception as e:
        return json.dumps({
            "connected": False,
            "error": str(e)
        }, indent=2)


# =============================================================================
# Patch primitives: surgical edits without full-body replacement
# =============================================================================
#
# joplin_update_note rewrites the entire note body on every call. Any caller
# bug — stale fetched state, shell-substitution corruption, transport-layer
# truncation of the inlined body — wipes history. The tools below let a caller
# express an edit as a section-scoped append/replace or a list of unique
# search/replace operations, so the rest of the note is never serialized
# through the caller and structurally cannot be lost.
#
# All three primitives:
#   - fetch the current body via api.get_note
#   - compute the new body deterministically in Python
#   - issue exactly one api.modify_note call with the new body
#   - share the same {"success": True, ...} / {"error": ...} envelope
# They are NOT atomic against concurrent edits; see the
# joplin_update_note_if_unchanged tool (separate proposal) for CAS semantics.


class PatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: str = Field(
        ...,
        description="Exact substring to find in the note body. Must occur exactly once.",
        min_length=1,
    )
    replace: str = Field(
        default="",
        description="Replacement text. Empty string deletes the search match.",
    )


class AppendToSectionInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                if "Extra data" in str(e) and v.endswith("}"):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note_id: str = Field(..., description="The ID of the note to update")
    section_heading: str = Field(
        ...,
        description="Heading text to locate, e.g. 'Done (recent)'. Matched against a Markdown heading line of the given level.",
        min_length=1,
    )
    content: str = Field(
        ...,
        description="Text to insert into the section. A trailing newline is added if missing.",
        min_length=1,
    )
    heading_level: int = Field(
        default=2,
        description="Markdown heading level to match (1-6). Default 2 (## Heading).",
        ge=1,
        le=6,
    )
    position: str = Field(
        default="bottom",
        description="'top' inserts immediately after the heading line, 'bottom' inserts at the end of the section.",
        pattern=r"^(top|bottom)$",
    )


class ReplaceSectionInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                if "Extra data" in str(e) and v.endswith("}"):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note_id: str = Field(..., description="The ID of the note to update")
    section_heading: str = Field(..., description="Heading text to locate", min_length=1)
    new_content: str = Field(
        ...,
        description="Replacement body for the section. The heading line itself is preserved.",
    )
    heading_level: int = Field(default=2, ge=1, le=6)


class ApplyPatchInput(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                if "Extra data" in str(e) and v.endswith("}"):
                    return json.loads(v[: e.pos])
                raise
        return v

    model_config = ConfigDict(extra="forbid")

    note_id: str = Field(..., description="The ID of the note to update")
    operations: List[PatchOperation] = Field(
        ...,
        description="Ordered list of search/replace operations. Each search string must occur exactly once in the current body. All operations are validated before any write; if any fails, no write occurs.",
        min_length=1,
    )


def _find_heading_line(body: str, heading: str, level: int) -> int:
    """Return the line index of the unique heading line, or raise.

    Match is: a line whose content (after stripping the leading '#' * level + space)
    equals `heading` after stripping surrounding whitespace.
    """
    prefix = "#" * level + " "
    matches = []
    for i, line in enumerate(body.splitlines()):
        if line.startswith(prefix):
            text = line[len(prefix):].strip()
            if text == heading.strip():
                matches.append(i)
    if not matches:
        raise LookupError(json.dumps({
            "error": "section_not_found",
            "details": {"heading": heading, "heading_level": level},
        }, indent=2))
    if len(matches) > 1:
        raise LookupError(json.dumps({
            "error": "section_ambiguous",
            "details": {
                "heading": heading,
                "heading_level": level,
                "match_lines": [m + 1 for m in matches],  # 1-indexed for humans
            },
        }, indent=2))
    return matches[0]


def _section_end_line(body: str, heading_line_idx: int, level: int) -> int:
    """Return the line index just past the end of the section started at heading_line_idx.

    A section ends at the next Markdown heading of equal-or-higher level (i.e.
    fewer-or-equal '#' characters), or at end-of-file.
    """
    lines = body.splitlines(keepends=False)
    for i in range(heading_line_idx + 1, len(lines)):
        line = lines[i]
        # Match any heading of level 1..`level`
        for L in range(1, level + 1):
            p = "#" * L + " "
            if line.startswith(p):
                return i
    return len(lines)


@mcp.tool(
    name="joplin_append_to_section",
    annotations={
        "title": "Append content to a section of a Joplin note",
        "readOnlyHint": False,
        "destructiveHint": False,
    }
)
def joplin_append_to_section(params: AppendToSectionInput) -> str:
    """Append text to a named section of a note without rewriting the rest.

    Locates the unique '#'*level + section_heading line in the current body,
    inserts `content` at the top or bottom of that section (delimited by the
    next equal-or-higher heading), and writes the result back via modify_note.

    Returns:
        JSON with success / error envelope.
    """
    try:
        with _with_lock() as api:
            note = api.get_note(params.note_id)
            body = note.body or ""
            try:
                head_idx = _find_heading_line(body, params.section_heading, params.heading_level)
            except LookupError as e:
                return str(e)
            end_idx = _section_end_line(body, head_idx, params.heading_level)

            lines = body.splitlines(keepends=False)
            insert_text = params.content
            if not insert_text.endswith("\n"):
                insert_text += "\n"
            insert_block = insert_text.rstrip("\n").split("\n")

            if params.position == "top":
                # Skip a single blank line immediately after the heading if present,
                # then insert; ensures a blank line separates heading from inserted block.
                ins = head_idx + 1
                if ins < len(lines) and lines[ins].strip() == "":
                    ins += 1
                new_lines = lines[:ins] + insert_block + [""] + lines[ins:]
            else:  # bottom
                # Trim trailing blank lines inside the section, then append, then re-pad.
                e = end_idx
                while e > head_idx + 1 and lines[e - 1].strip() == "":
                    e -= 1
                new_lines = lines[:e] + [""] + insert_block + [""] + lines[end_idx:]

            new_body = "\n".join(new_lines)
            # Preserve original trailing newline state
            if body.endswith("\n") and not new_body.endswith("\n"):
                new_body += "\n"

            api.modify_note(params.note_id, body=new_body)
            return json.dumps({
                "success": True,
                "note_id": params.note_id,
                "section_heading": params.section_heading,
                "position": params.position,
                "appended_chars": len(params.content),
                "body_chars_before": len(body),
                "body_chars_after": len(new_body),
                "message": "Section appended successfully",
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_replace_section",
    annotations={
        "title": "Replace the body of a named section of a Joplin note",
        "readOnlyHint": False,
        "destructiveHint": False,
    }
)
def joplin_replace_section(params: ReplaceSectionInput) -> str:
    """Replace everything between a heading and the next equal-or-higher heading.

    The heading line itself is preserved. Sibling sections are not touched.

    Returns:
        JSON with success / error envelope.
    """
    try:
        with _with_lock() as api:
            note = api.get_note(params.note_id)
            body = note.body or ""
            try:
                head_idx = _find_heading_line(body, params.section_heading, params.heading_level)
            except LookupError as e:
                return str(e)
            end_idx = _section_end_line(body, head_idx, params.heading_level)

            lines = body.splitlines(keepends=False)
            new_block = params.new_content.rstrip("\n").split("\n") if params.new_content else []
            # Pad: blank line after heading, blank line before next section (if not EOF)
            new_section = [lines[head_idx], ""] + new_block
            if end_idx < len(lines):
                new_section += [""]

            new_lines = lines[:head_idx] + new_section + lines[end_idx:]
            new_body = "\n".join(new_lines)
            if body.endswith("\n") and not new_body.endswith("\n"):
                new_body += "\n"

            api.modify_note(params.note_id, body=new_body)
            return json.dumps({
                "success": True,
                "note_id": params.note_id,
                "section_heading": params.section_heading,
                "body_chars_before": len(body),
                "body_chars_after": len(new_body),
                "message": "Section replaced successfully",
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool(
    name="joplin_apply_patch",
    annotations={
        "title": "Apply a list of unique search/replace operations to a Joplin note",
        "readOnlyHint": False,
        "destructiveHint": False,
    }
)
def joplin_apply_patch(params: ApplyPatchInput) -> str:
    """Apply ordered search/replace operations atomically.

    Each operation's `search` string must occur exactly once in the body at the
    time it is applied. Operations are applied in order, so an earlier
    replacement can create context for a later search. If any operation fails
    (search_not_found or search_not_unique), no write occurs.

    Returns:
        JSON with success / error envelope.
    """
    try:
        with _with_lock() as api:
            note = api.get_note(params.note_id)
            body = note.body or ""
            original_len = len(body)

            new_body = body
            for idx, op in enumerate(params.operations):
                count = new_body.count(op.search)
                if count == 0:
                    return json.dumps({
                        "error": "search_not_found",
                        "details": {
                            "op_index": idx,
                            "search_preview": op.search[:80],
                            "search_len": len(op.search),
                        },
                    }, indent=2)
                if count > 1:
                    return json.dumps({
                        "error": "search_not_unique",
                        "details": {
                            "op_index": idx,
                            "search_preview": op.search[:80],
                            "match_count": count,
                        },
                    }, indent=2)
                new_body = new_body.replace(op.search, op.replace, 1)

            api.modify_note(params.note_id, body=new_body)
            return json.dumps({
                "success": True,
                "note_id": params.note_id,
                "operations_applied": len(params.operations),
                "body_chars_before": original_len,
                "body_chars_after": len(new_body),
                "message": "Patch applied successfully",
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    if not JOPLIN_SERVER_EMAIL or not JOPLIN_SERVER_PASSWORD:
        print(
            "WARNING: JOPLIN_SERVER_EMAIL and JOPLIN_SERVER_PASSWORD not set.\n"
            "Set them with:\n"
            "  export JOPLIN_SERVER_EMAIL='your-email'\n"
            "  export JOPLIN_SERVER_PASSWORD='your-password'\n",
            file=sys.stderr
        )

    mcp.run()
