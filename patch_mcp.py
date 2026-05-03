"""
Patch joplin_server_mcp.py to handle the case where FastMCP passes tool
arguments as a JSON string instead of a parsed dict (happens with large
bodies due to supergateway stdio framing). Adds a model_validator to all
*Input pydantic models so they accept either a dict or a JSON string.
"""
import re

PATCH_IMPORT = "from pydantic import BaseModel, Field, ConfigDict, model_validator"

VALIDATOR = '''\
    @model_validator(mode="before")
    @classmethod
    def _parse_if_string(cls, v):
        if isinstance(v, str):
            import json
            return json.loads(v)
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

with open("/app/joplin_server_mcp.py", "w") as f:
    f.write(src)

print("patch applied")
