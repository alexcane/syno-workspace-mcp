"""Synology Drive tools (read-only part, milestone 2)."""

from __future__ import annotations

from typing import Annotated

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from syno_workspace_mcp.clients.drive import (
    DriveClient,
    DriveFileInfo,
    DriveFileList,
    FileType,
    SortBy,
    SortDirection,
)
from syno_workspace_mcp.errors import SynologyError, to_tool_error

DRIVE_CLIENT_KEY = "drive"

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

DrivePath = Annotated[
    str,
    Field(
        description="Absolute path in Drive. Use '/mydrive' for the private folder, "
        "'/team-folders/NAME' for team folders."
    ),
]


def _client(ctx: Context) -> DriveClient:
    client = ctx.lifespan_context.get(DRIVE_CLIENT_KEY)
    if not isinstance(client, DriveClient):  # pragma: no cover - wiring error
        raise RuntimeError("Drive client is not initialised")
    return client


def register_drive_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="drive_list_files", annotations=READ_ONLY, tags={"drive"})
    async def drive_list_files(
        ctx: Context,
        folder_path: DrivePath = "/mydrive",
        limit: Annotated[
            int, Field(ge=1, le=1000, description="Maximum number of items to return")
        ] = 100,
        offset: Annotated[int, Field(ge=0, description="Pagination offset")] = 0,
        sort_by: Annotated[SortBy, Field(description="Sort field")] = "name",
        sort_direction: SortDirection = "ASC",
        file_type: Annotated[FileType, Field(description="Filter by type")] = "all",
        pattern: Annotated[
            str | None, Field(description="Glob pattern filter, e.g. '*.osheet' or '*report*'")
        ] = None,
    ) -> DriveFileList:
        """List files and folders in a Synology Drive path.

        Supports pagination, sorting, and glob filtering.
        """
        try:
            return await _client(ctx).list_files(
                folder_path,
                limit=limit,
                offset=offset,
                sort_by=sort_by,
                sort_direction=sort_direction,
                file_type=file_type,
                pattern=pattern,
            )
        except SynologyError as error:
            raise to_tool_error(error) from error

    @mcp.tool(name="drive_get_file_info", annotations=READ_ONLY, tags={"drive"})
    async def drive_get_file_info(
        ctx: Context,
        path: Annotated[
            str,
            Field(
                description="File or folder path, e.g. '/mydrive/reports/Q1.osheet', "
                "or a numeric file ID"
            ),
        ],
    ) -> DriveFileInfo:
        """Get detailed metadata (size, owner, ACL, labels) for a Drive file or folder."""
        try:
            return await _client(ctx).get_file_info(path)
        except SynologyError as error:
            raise to_tool_error(error) from error

    @mcp.tool(name="drive_search_files", annotations=READ_ONLY, tags={"drive"})
    async def drive_search_files(
        ctx: Context,
        query: Annotated[str, Field(min_length=1, description="Search keyword")],
        folder_path: Annotated[
            str, Field(description="Scope the search to this path")
        ] = "/mydrive",
        extension: Annotated[
            str | None, Field(description="Filter by extension, e.g. 'osheet', 'odoc', 'pdf'")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> DriveFileList:
        """Search for files across Synology Drive by name or keyword.

        Supports an optional extension filter.
        """
        try:
            return await _client(ctx).search(
                query, folder_path=folder_path, limit=limit, extension=extension
            )
        except SynologyError as error:
            raise to_tool_error(error) from error
