"""Synology Drive tools (11 tools, same names and inputs as upstream)."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from syno_workspace_mcp.clients.drive import (
    MAX_TRANSFER_BYTES,
    ConflictAction,
    DriveClient,
    DriveDeleteResult,
    DriveFileInfo,
    DriveFileList,
    DriveFolderResult,
    DriveLabel,
    DriveLabelResult,
    DriveMoveResult,
    DriveSharingLinkResult,
    DriveUploadResult,
    FileType,
    SharePermission,
    SortBy,
    SortDirection,
)
from syno_workspace_mcp.errors import SynologyError, to_tool_error
from syno_workspace_mcp.tools.common import (
    DESTRUCTIVE,
    READ_ONLY,
    WRITE,
    Confirm,
    require_confirm,
)

DRIVE_CLIENT_KEY = "drive"

# Creates or updates the link of the file: repeating the call gives the same result.
SHARE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

PathOrId = Annotated[str, Field(description="File or folder path in Drive, or a numeric file ID")]


class DriveDownloadResult(BaseModel):
    file_name: str
    mime_type: str
    size: int
    content_base64: str


class DriveLabelList(BaseModel):
    labels: list[DriveLabel]


def decode_base64(content: str) -> bytes:
    # Base64 grows data by 4/3: reject oversized payloads before decoding them.
    if len(content) > MAX_TRANSFER_BYTES * 4 // 3 + 4:
        raise ToolError(f"file is larger than the {MAX_TRANSFER_BYTES} bytes limit")
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ToolError("content_base64 is not valid base64") from error


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

    @mcp.tool(name="drive_download_file", annotations=READ_ONLY, tags={"drive"})
    async def drive_download_file(ctx: Context, path: PathOrId) -> DriveDownloadResult:
        """Download a file from Synology Drive and return it as base64-encoded content.

        Files larger than 50 MB are refused.
        """
        try:
            result = await _client(ctx).download(path)
        except SynologyError as error:
            raise to_tool_error(error) from error
        return DriveDownloadResult(
            file_name=result.file_name,
            mime_type=result.mime_type,
            size=len(result.content),
            content_base64=base64.b64encode(result.content).decode("ascii"),
        )

    @mcp.tool(name="drive_list_labels", annotations=READ_ONLY, tags={"drive"})
    async def drive_list_labels(ctx: Context) -> DriveLabelList:
        """List all labels defined in Synology Drive (id, name, color)."""
        try:
            return DriveLabelList(labels=await _client(ctx).list_labels())
        except SynologyError as error:
            raise to_tool_error(error) from error

    @mcp.tool(name="drive_get_sharing_link", annotations=SHARE, tags={"drive"})
    async def drive_get_sharing_link(
        ctx: Context,
        path: PathOrId,
        permission: Annotated[
            SharePermission,
            Field(description="'view' (preview only), 'download' (view and download) or 'edit'"),
        ] = "view",
        password: Annotated[str | None, Field(description="Optional password protection")] = None,
        expire_days: Annotated[
            int | None, Field(ge=1, le=365, description="Link expiry in days; omit for none")
        ] = None,
    ) -> DriveSharingLinkResult:
        """Create or update the sharing link of a Drive file or folder.

        The link gets exactly the requested permission, password and expiry:
        omitting them removes a previous password or expiry.
        """
        try:
            return await _client(ctx).get_sharing_link(
                path, permission=permission, password=password, expire_days=expire_days
            )
        except SynologyError as error:
            raise to_tool_error(error) from error

    @mcp.tool(name="drive_upload_file", annotations=WRITE, tags={"drive"})
    async def drive_upload_file(
        ctx: Context,
        dest_folder_path: Annotated[
            str, Field(description="Destination folder, e.g. '/mydrive/uploads'")
        ],
        file_name: Annotated[
            str, Field(description="File name including extension, e.g. 'report.pdf'")
        ],
        content_base64: Annotated[str, Field(description="Base64-encoded file content")],
        mime_type: Annotated[
            str, Field(description="MIME type, e.g. 'application/pdf', 'text/csv'")
        ] = "application/octet-stream",
        conflict_action: Annotated[
            ConflictAction,
            Field(
                description="If the file exists: 'version' replaces it and keeps the old "
                "content in version history, 'autorename' stores the upload under a new name"
            ),
        ] = "version",
    ) -> DriveUploadResult:
        """Upload a file to Synology Drive from a base64-encoded payload (50 MB max)."""
        content = decode_base64(content_base64)
        try:
            return await _client(ctx).upload(
                dest_folder_path,
                file_name,
                content,
                mime_type=mime_type,
                conflict_action=conflict_action,
            )
        except SynologyError as error:
            raise to_tool_error(error) from error

    @mcp.tool(name="drive_create_folder", annotations=WRITE, tags={"drive"})
    async def drive_create_folder(
        ctx: Context,
        folder_path: Annotated[str, Field(description="Parent folder, e.g. '/mydrive/projects'")],
        name: Annotated[str, Field(description="New folder name")],
        force_parent: Annotated[bool, Field(description="Create missing parent folders")] = False,
    ) -> DriveFolderResult:
        """Create a folder in Synology Drive, optionally creating missing parents.

        If the name is taken, Drive picks a new name: check `folder_path` in the result.
        """
        try:
            return await _client(ctx).create_folder(folder_path, name, force_parent=force_parent)
        except SynologyError as error:
            raise to_tool_error(error) from error

    @mcp.tool(name="drive_add_label", annotations=WRITE, tags={"drive"})
    async def drive_add_label(
        ctx: Context,
        path: PathOrId,
        label_name: Annotated[
            str, Field(description="Name of an existing label (see drive_list_labels)")
        ],
    ) -> DriveLabelResult:
        """Add an existing label to a file or folder in Synology Drive."""
        try:
            return await _client(ctx).add_label(path, label_name)
        except SynologyError as error:
            raise to_tool_error(error) from error

    @mcp.tool(name="drive_move_file", annotations=DESTRUCTIVE, tags={"drive"})
    async def drive_move_file(
        ctx: Context,
        path: Annotated[str, Field(description="Source file/folder path or ID")],
        dest_folder_path: Annotated[
            str, Field(description="Destination folder path or ID (the current folder to rename)")
        ],
        new_name: Annotated[
            str | None, Field(description="New name after the move (optional rename)")
        ] = None,
        conflict_action: Annotated[
            ConflictAction,
            Field(
                description="If the name exists in the destination: 'autorename' keeps both, "
                "'version' replaces the existing item"
            ),
        ] = "autorename",
        confirm: Confirm = False,
    ) -> DriveMoveResult:
        """Move or rename a file/folder in Synology Drive. Requires confirm=true."""
        renamed = f" as '{new_name}'" if new_name else ""
        require_confirm(confirm, f"move '{path}' to '{dest_folder_path}'{renamed}")
        try:
            return await _client(ctx).move(
                path, dest_folder_path, new_name=new_name, conflict_action=conflict_action
            )
        except SynologyError as error:
            raise to_tool_error(error) from error

    @mcp.tool(name="drive_delete_file", annotations=DESTRUCTIVE, tags={"drive"})
    async def drive_delete_file(
        ctx: Context,
        path: Annotated[str, Field(description="File or folder path or ID to delete")],
        permanent: Annotated[
            bool,
            Field(description="True deletes permanently; false moves it to the Drive trash"),
        ] = False,
        confirm: Confirm = False,
    ) -> DriveDeleteResult:
        """Delete a file or folder in Synology Drive (trash by default). Requires confirm=true."""
        mode = "permanently" if permanent else "(to trash)"
        require_confirm(confirm, f"delete '{path}' {mode}")
        try:
            return await _client(ctx).delete(path, permanent=permanent)
        except SynologyError as error:
            raise to_tool_error(error) from error
