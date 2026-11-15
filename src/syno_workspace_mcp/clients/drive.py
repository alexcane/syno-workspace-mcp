"""Synology Drive client (`SYNO.SynologyDrive.Files`), read operations."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from syno_workspace_mcp.clients.dsm import DsmClient, Params
from syno_workspace_mcp.errors import DsmNetworkError, InvalidInputError

FILES_API = "SYNO.SynologyDrive.Files"
FILES_VERSION = 2

DRIVE_ROOTS = ("/mydrive", "/team-folders")
_FILE_ID = re.compile(r"\d+")

SortBy = Literal["name", "size", "user", "modified", "created"]
SortDirection = Literal["ASC", "DESC"]
FileType = Literal["all", "file", "dir"]


# Path guard -----------------------------------------------------------------


def normalize_drive_path(path: str) -> str:
    """Reject traversal and paths outside the Drive roots; return a clean path."""
    raw = path.strip()
    if not raw:
        raise InvalidInputError("path must not be empty")
    segments = re.split(r"[\\/]+", raw)
    if ".." in segments:
        raise InvalidInputError("path must not contain '..' segments")
    if not raw.startswith("/"):
        raise InvalidInputError(f"path must be absolute and start with one of {DRIVE_ROOTS}")
    cleaned = "/" + "/".join(seg for seg in segments if seg not in ("", "."))
    if not any(cleaned == root or cleaned.startswith(root + "/") for root in DRIVE_ROOTS):
        raise InvalidInputError(f"path must start with one of {DRIVE_ROOTS}")
    return cleaned


def normalize_path_or_id(value: str) -> str:
    """Accept a Drive path or a numeric file ID (passed through unchanged)."""
    stripped = value.strip()
    if _FILE_ID.fullmatch(stripped):
        return stripped
    return normalize_drive_path(stripped)


# Raw DSM shapes (tolerant) --------------------------------------------------


class _RawOwner(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    nickname: str | None = None


class _RawLabel(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    label_id: str | None = None
    name: str | None = None


class _RawFile(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    file_id: str
    name: str
    path: str = ""
    display_path: str | None = None
    dsm_path: str | None = None
    type: Literal["file", "dir"]
    size: int = 0
    modified_time: int | None = None
    created_time: int | None = None
    owner: _RawOwner | None = None
    shared: bool = False
    capabilities: dict[str, bool] = Field(default_factory=dict)
    labels: list[str | _RawLabel] = Field(default_factory=list)


class _RawFileList(BaseModel):
    model_config = ConfigDict(extra="allow")

    total: int = 0
    items: list[_RawFile] = Field(default_factory=list)


# Output shapes (same fields as upstream) ------------------------------------


class DriveFile(BaseModel):
    id: str
    name: str
    path: str
    type: Literal["file", "dir"]
    size: int = Field(description="Size in bytes; 0 for directories")
    modified: str = Field(description="ISO 8601, empty when unknown")
    created: str = Field(description="ISO 8601, empty when unknown")
    owner: str
    extension: str = Field(description="Extension without the dot, empty for directories")
    is_shared: bool


class DriveAcl(BaseModel):
    append: bool
    delete: bool = Field(alias="del")
    exec: bool
    read: bool
    write: bool

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class DrivePermissions(BaseModel):
    acl: DriveAcl
    is_owner: bool


class DriveFileInfo(DriveFile):
    real_path: str
    perm: DrivePermissions
    labels: list[str]


class DriveFileList(BaseModel):
    total: int
    offset: int
    files: list[DriveFile]


def _iso(timestamp: int | None) -> str:
    if not timestamp:
        return ""
    moment = datetime.fromtimestamp(timestamp, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _to_file(raw: _RawFile) -> DriveFile:
    owner = (raw.owner.name or raw.owner.nickname or "") if raw.owner else ""
    extension = raw.name.rsplit(".", 1)[1] if raw.type == "file" and "." in raw.name else ""
    return DriveFile(
        id=raw.file_id,
        name=raw.name,
        path=raw.display_path or raw.path,
        type=raw.type,
        size=raw.size,
        modified=_iso(raw.modified_time),
        created=_iso(raw.created_time),
        owner=owner,
        extension=extension,
        is_shared=raw.shared,
    )


def _to_file_info(raw: _RawFile) -> DriveFileInfo:
    caps = raw.capabilities
    labels = [
        label if isinstance(label, str) else (label.name or label.label_id or "")
        for label in raw.labels
    ]
    return DriveFileInfo(
        **_to_file(raw).model_dump(),
        real_path=raw.dsm_path or raw.display_path or raw.path,
        perm=DrivePermissions(
            acl=DriveAcl(
                append=caps.get("can_write", False),
                delete=caps.get("can_delete", False),
                exec=False,
                read=caps.get("can_read", False),
                write=caps.get("can_write", False),
            ),
            is_owner=caps.get("can_organize", False),
        ),
        labels=labels,
    )


def _parse[T: BaseModel](model: type[T], data: dict[str, Any], what: str) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise DsmNetworkError(f"unexpected DSM response shape for {what}") from error


# Client ---------------------------------------------------------------------


class DriveClient:
    def __init__(self, dsm: DsmClient) -> None:
        self._dsm = dsm

    async def list_files(
        self,
        folder_path: str = "/mydrive",
        *,
        limit: int = 100,
        offset: int = 0,
        sort_by: SortBy = "name",
        sort_direction: SortDirection = "ASC",
        file_type: FileType = "all",
        pattern: str | None = None,
    ) -> DriveFileList:
        params: Params = {
            "path": normalize_drive_path(folder_path),
            "offset": offset,
            "limit": limit,
            "sort_by": sort_by,
            "sort_direction": sort_direction,
        }
        if file_type != "all":
            params["filter"] = json.dumps({"type": file_type})
        if pattern is not None:
            params["pattern"] = pattern
        data = await self._dsm.call(FILES_API, "list", FILES_VERSION, params)
        raw = _parse(_RawFileList, data, "list")
        return DriveFileList(total=raw.total, offset=offset, files=[_to_file(f) for f in raw.items])

    async def get_file_info(self, path: str) -> DriveFileInfo:
        params: Params = {"path": normalize_path_or_id(path)}
        data = await self._dsm.call(FILES_API, "get", FILES_VERSION, params)
        return _to_file_info(_parse(_RawFile, data, "get"))

    async def search(
        self,
        query: str,
        *,
        folder_path: str = "/mydrive",
        limit: int = 50,
        extension: str | None = None,
    ) -> DriveFileList:
        if not query.strip():
            raise InvalidInputError("query must not be empty")
        params: Params = {
            "keyword": query,
            "path": normalize_drive_path(folder_path),
            "limit": limit,
        }
        if extension is not None:
            params["extension"] = extension
        data = await self._dsm.call(FILES_API, "search", FILES_VERSION, params)
        raw = _parse(_RawFileList, data, "search")
        return DriveFileList(total=raw.total, offset=0, files=[_to_file(f) for f in raw.items])
