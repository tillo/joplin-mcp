"""
Patch joplin_server_mcp.py:

1. Handle the case where FastMCP passes tool arguments as a JSON string
   instead of a parsed dict (happens with large bodies due to supergateway
   stdio framing) — adds a model_validator to all *Input pydantic models so
   they accept either a dict or a JSON string.

2. Force UTF-8 decoding of joppy's HTTP responses. Joplin Server's
   /api/items/.../content endpoint returns the note body as text/plain with
   no charset, so `requests` leaves Response.encoding unset and joppy's
   `response.text` mis-decodes the UTF-8 body (mojibake that compounds on
   every get/update round-trip). A response hook on joppy's session pins the
   encoding to utf-8.
"""
import re

PATCH_IMPORT = "from pydantic import BaseModel, Field, ConfigDict, model_validator"

VALIDATOR = '''\
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
                if "Extra data" in str(e) and v.endswith("}"):
                    return json.loads(v[: e.pos])
                raise
        return v
'''

with open("/app/joplin_server_mcp.py", "r") as f:
    src = f.read()

# Replace pydantic import line to add model_validator
src = src.replace(
    "from pydantic import BaseModel, Field, ConfigDict",
    PATCH_IMPORT,
)

# Insert validator after each `class *Input(BaseModel):` definition line
src = re.sub(
    r"(class \w+Input\(BaseModel\):)",
    lambda m: m.group(0) + "\n" + VALIDATOR,
    src,
)

# Force UTF-8 on every joppy response (see module docstring, point 2).
UTF8_FIX = '''
# --- injected by patch_mcp.py: pin joppy responses to utf-8 -------------------
# Joplin Server returns note content as text/plain without a charset, so
# requests leaves Response.encoding unset and joppy's `.text` mis-decodes the
# UTF-8 body. A response hook forces utf-8 so note bodies round-trip cleanly.
import joppy.server_api as _joppy_server_api

def _joppy_force_utf8(_resp, *_args, **_kwargs):
    _resp.encoding = "utf-8"
    return _resp

_joppy_server_api.SESSION.hooks.setdefault("response", [])
if _joppy_force_utf8 not in _joppy_server_api.SESSION.hooks["response"]:
    _joppy_server_api.SESSION.hooks["response"].append(_joppy_force_utf8)
# -----------------------------------------------------------------------------
'''

_joppy_import = "from joppy.server_api import ServerApi, LockError"
if _joppy_import not in src:
    raise SystemExit(
        "patch_mcp.py: joppy import line not found — upstream changed, "
        "the UTF-8 fix injection point must be updated"
    )
src = src.replace(_joppy_import, _joppy_import + "\n" + UTF8_FIX, 1)

with open("/app/joplin_server_mcp.py", "w") as f:
    f.write(src)

print("patch applied")
