"""Annotations and guards shared by every tool module."""

from __future__ import annotations

from typing import Annotated

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

Confirm = Annotated[
    bool, Field(description="Must be true to execute; otherwise nothing is changed")
]


def require_confirm(confirm: bool, operation: str) -> None:
    """Refuse before any network call when the caller did not confirm."""
    if confirm is not True:
        raise ToolError(f"This operation requires confirm=true to execute: {operation}")
