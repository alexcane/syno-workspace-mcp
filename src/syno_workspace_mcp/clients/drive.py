"""Synology Drive client (`SYNO.SynologyDrive.*`).

The write operations follow the requests the Drive web UI (package 4.0.3) sends:
parameter values are JSON-encoded, move and delete run as background tasks.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from syno_workspace_mcp.clients.dsm import DsmClient, DsmUploadFile, Params
from syno_workspace_mcp.errors import DsmApiError, DsmNetworkError, InvalidInputError
from syno_workspace_mcp.logging import forget_secret, register_secret

FILES_API = "SYNO.SynologyDrive.Files"
FILES_VERSION = 2
LABELS_API = "SYNO.SynologyDrive.Labels"
LABELS_VERSION = 1
TASKS_API = "SYNO.SynologyDrive.Tasks"
TASKS_VERSION = 1
ADV_SHARING_API = "SYNO.SynologyDrive.AdvanceSharing"
ADV_SHARING_VERSION = 1

FILE_NOT_FOUND = 1003
SHARING_LINK_NOT_CREATED = 1036

# Upstream recommends staying under 50 MB: the payload travels base64-encoded in MCP.
MAX_TRANSFER_BYTES = 50 * 1024 * 1024

DRIVE_ROOTS = ("/mydrive", "/team-folders")
_FILE_ID = re.compile(r"(?:id:)?(\d+)")

SortBy = Literal["name", "size", "user", "modified", "created"]
SortDirection = Literal["ASC", "DESC"]
FileType = Literal["all", "file", "dir"]
# Values seen in the Drive web UI; upstream's `skip` is not used there.
ConflictAction = Literal["version", "autorename"]
SharePermission = Literal["view", "download", "edit"]

# Drive link roles: `previewer` cannot download, `viewer` can.
_SHARE_ROLES: dict[str, str] = {"view": "previewer", "download": "viewer", "edit": "editor"}
_TASK_RUNNING = frozenset({"in_progress", "waiting", "pending", "processing"})

# Indirection so tests can drive the task deadline without touching the event loop clock.
_monotonic = time.monotonic


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
    """Accept a Drive path or a file ID (`123` or `id:123`); IDs become `id:123`."""
    stripped = value.strip()
    match = _FILE_ID.fullmatch(stripped)
    if match:
        return f"id:{match[1]}"
    return normalize_drive_path(stripped)


def validate_name(name: str) -> str:
    """A single path segment: no separator, no `.`/`..`."""
    cleaned = name.strip()
    if not cleaned:
        raise InvalidInputError("name must not be empty")
    if "/" in cleaned or "\\" in cleaned:
        raise InvalidInputError("name must not contain '/' or '\\'")
    if cleaned in (".", ".."):
        raise InvalidInputError("name must not be '.' or '..'")
    return cleaned


def _j(value: object) -> str:
    """Drive APIs declare `requestFormat: JSON`: each value is parsed as JSON."""
    return json.dumps(value)


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
    parent_id: str | None = None
    owner: _RawOwner | None = None
    shared: bool = False
    capabilities: dict[str, bool] = Field(default_factory=dict)
    labels: list[str | _RawLabel] = Field(default_factory=list)
    # For an .osheet this is its Spreadsheet ID (the `/oo/r/<id>` of the Office URL).
    permanent_link: str | None = None


class _RawLabelDef(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    label_id: str
    name: str
    color: str = ""


class _RawLabelList(BaseModel):
    model_config = ConfigDict(extra="allow")

    items: list[_RawLabelDef] = Field(default_factory=list)


class _RawTaskTarget(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    file_id: str


class _RawTaskResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    errors: list[Any] | None = None
    targets: list[_RawTaskTarget] = Field(default_factory=list)


class _RawTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    result: _RawTaskResult = Field(default_factory=_RawTaskResult)


class _RawSharing(BaseModel):
    model_config = ConfigDict(extra="allow")

    sharing_link: str
    url: str | None = None


class _RawFileList(BaseModel):
    model_config = ConfigDict(extra="allow")

    total: int = 0
    items: list[_RawFile] = Field(default_factory=list)


_EXTENSION_GLOB = re.compile(r"\*\.([A-Za-z0-9]+)")


def _list_filter(
    file_type: FileType, pattern: str | None
) -> tuple[dict[str, list[str]], str | None]:
    """Drive's `filter` for Files.list, plus a glob Drive cannot apply (matched locally).

    Verified on the NAS: `type` only works as a list, `pattern` is ignored, and
    `extensions` filters files but lets every folder through.
    """
    list_filter: dict[str, list[str]] = {"type": [file_type]} if file_type != "all" else {}
    if pattern is None:
        return list_filter, None
    match = _EXTENSION_GLOB.fullmatch(pattern.strip())
    if match and file_type != "dir":
        return {"type": ["file"], "extensions": [match[1].lower()]}, None
    return list_filter, pattern


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


class DriveUploadResult(BaseModel):
    success: bool
    file_id: str
    file_path: str
    file_name: str


class DriveDownload(BaseModel):
    file_name: str
    mime_type: str
    content: bytes


class DriveFolderResult(BaseModel):
    success: bool
    folder_id: str
    folder_path: str


class DriveMoveResult(BaseModel):
    dry_run: bool
    success: bool
    new_path: str


class DriveDeleteResult(BaseModel):
    success: bool


class DriveLabel(BaseModel):
    id: str
    name: str
    color: str = Field(description="Hex color as stored by Drive, e.g. '#DCE1E6'")


class DriveLabelResult(BaseModel):
    success: bool


class DriveSharingLinkResult(BaseModel):
    link: str
    permission: SharePermission
    expires_at: str | None


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
    def __init__(
        self, dsm: DsmClient, *, poll_interval: float = 0.5, task_timeout: float = 120.0
    ) -> None:
        self._dsm = dsm
        self._poll_interval = poll_interval
        self._task_timeout = task_timeout

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
        raw = await self._list(
            folder_path, limit, offset, sort_by, sort_direction, file_type, pattern
        )
        return DriveFileList(total=raw.total, offset=offset, files=[_to_file(f) for f in raw.items])

    async def get_file_info(self, path: str) -> DriveFileInfo:
        return _to_file_info(await self._get_raw(normalize_path_or_id(path)))

    async def list_with_links(
        self,
        folder_path: str,
        *,
        limit: int,
        offset: int,
        file_type: FileType = "all",
        pattern: str | None = None,
    ) -> tuple[int, list[tuple[DriveFile, str | None]]]:
        """Like `list_files`, plus each item's `permanent_link`."""
        raw = await self._list(folder_path, limit, offset, "name", "ASC", file_type, pattern)
        return raw.total, [(_to_file(f), f.permanent_link or None) for f in raw.items]

    async def get_with_link(self, path: str) -> tuple[DriveFile, str | None]:
        """A file and its `permanent_link` (the Spreadsheet ID for an .osheet)."""
        raw = await self._get_raw(normalize_path_or_id(path))
        return _to_file(raw), raw.permanent_link or None

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

    # Write operations -------------------------------------------------------

    async def create_folder(
        self, folder_path: str, name: str, *, force_parent: bool = False
    ) -> DriveFolderResult:
        parent = normalize_drive_path(folder_path)
        folder_name = validate_name(name)
        if force_parent:
            await self._ensure_folder(parent)
        raw = await self._create_folder(f"{parent}/{folder_name}")
        return DriveFolderResult(
            success=True, folder_id=raw.file_id, folder_path=raw.display_path or raw.path
        )

    async def upload(
        self,
        dest_folder_path: str,
        file_name: str,
        content: bytes,
        *,
        mime_type: str = "application/octet-stream",
        conflict_action: ConflictAction = "version",
    ) -> DriveUploadResult:
        dest = normalize_drive_path(dest_folder_path)
        name = validate_name(file_name)
        # Multipart fields are sent as plain strings, like the web UI does.
        fields = {"path": f"{dest}/{name}", "type": "file", "conflict_action": conflict_action}
        data = await self._dsm.upload(
            FILES_API,
            "upload",
            FILES_VERSION,
            fields,
            DsmUploadFile(name=name, content=content, mime_type=mime_type),
            max_bytes=MAX_TRANSFER_BYTES,
        )
        raw = _parse(_RawFile, data, "upload")
        return DriveUploadResult(
            success=True,
            file_id=raw.file_id,
            file_path=raw.display_path or raw.path,
            file_name=raw.name,
        )

    async def download(self, path: str) -> DriveDownload:
        ref = normalize_path_or_id(path)
        params: Params = {
            "files": _j([ref]),
            "force_download": "true",
            "json_error": "true",
            "download_type": _j("download"),
        }
        result = await self._dsm.download(
            FILES_API, "download", FILES_VERSION, params, max_bytes=MAX_TRANSFER_BYTES
        )
        fallback = ref.rsplit("/", 1)[-1] if not ref.startswith("id:") else "download"
        return DriveDownload(
            file_name=_filename(result.content_disposition) or fallback,
            mime_type=result.content_type.split(";", 1)[0].strip(),
            content=result.content,
        )

    async def list_labels(self) -> list[DriveLabel]:
        data = await self._dsm.call(LABELS_API, "list", LABELS_VERSION, {}, http_method="POST")
        raw = _parse(_RawLabelList, data, "list_labels")
        return [
            DriveLabel(id=item.label_id, name=item.name, color=item.color) for item in raw.items
        ]

    async def add_label(self, path: str, label_name: str) -> DriveLabelResult:
        ref = normalize_path_or_id(path)
        wanted = label_name.strip()
        if not wanted:
            raise InvalidInputError("label_name must not be empty")
        labels = await self.list_labels()
        matches = [label for label in labels if label.name == wanted] or [
            label for label in labels if label.name.casefold() == wanted.casefold()
        ]
        if len(matches) != 1:
            available = ", ".join(sorted(label.name for label in labels)) or "none"
            reason = "is ambiguous" if matches else "does not exist"
            raise InvalidInputError(f"label {wanted!r} {reason}; available labels: {available}")
        params: Params = {
            "files": _j([ref]),
            "labels": _j([{"action": "add", "label_id": matches[0].id}]),
        }
        await self._dsm.call(FILES_API, "label", FILES_VERSION, params, http_method="POST")
        return DriveLabelResult(success=True)

    async def get_sharing_link(
        self,
        path: str,
        *,
        permission: SharePermission = "view",
        password: str | None = None,
        expire_days: int | None = None,
    ) -> DriveSharingLinkResult:
        ref = normalize_path_or_id(path)
        if password:
            register_secret(password)
        try:
            sharing = await self._get_or_create_sharing(ref)
            due_date = int(time.time()) + expire_days * 86400 if expire_days else 0
            # Always send password and expiry so the link matches this request exactly:
            # an empty password and a zero due date remove a previous protection.
            params: Params = {
                "path": _j(ref),
                "sharing_link": _j(sharing.sharing_link),
                "role": _j(_SHARE_ROLES[permission]),
                "protect_password": _j(password or ""),
                "due_date": due_date,
            }
            await self._dsm.call(
                ADV_SHARING_API, "update", ADV_SHARING_VERSION, params, http_method="POST"
            )
        finally:
            if password:
                forget_secret(password)
        if not sharing.url:
            raise DsmNetworkError("DSM did not return the sharing link URL", api=ADV_SHARING_API)
        return DriveSharingLinkResult(
            link=sharing.url, permission=permission, expires_at=_iso(due_date) or None
        )

    async def move(
        self,
        path: str,
        dest_folder_path: str,
        *,
        new_name: str | None = None,
        conflict_action: ConflictAction = "autorename",
    ) -> DriveMoveResult:
        source = await self._get_raw(normalize_path_or_id(path))
        dest = await self._get_raw(normalize_path_or_id(dest_folder_path))
        if dest.type != "dir":
            raise InvalidInputError("dest_folder_path must be a folder")
        name = validate_name(new_name) if new_name is not None else None
        same_folder = source.parent_id == dest.file_id
        if same_folder and (name is None or name == source.name):
            raise InvalidInputError("nothing to do: the item is already in this folder")

        file_ref = f"id:{source.file_id}"
        if not same_folder:
            params: Params = {
                "to_parent_folder": _j(f"id:{dest.file_id}"),
                "files": _j([file_ref]),
                "conflict_action": _j(conflict_action),
            }
            await self._run_task("move", params)
        if name is not None and name != source.name:
            data = await self._dsm.call(
                FILES_API,
                "update",
                FILES_VERSION,
                {"path": _j(file_ref), "name": _j(name)},
                http_method="POST",
            )
            moved = _parse(_RawFile, data, "update")
        else:
            moved = await self._get_raw(file_ref)
        return DriveMoveResult(
            dry_run=False, success=True, new_path=moved.display_path or moved.path
        )

    async def delete(self, path: str, *, permanent: bool = False) -> DriveDeleteResult:
        ref = normalize_path_or_id(path)
        await self._run_task("delete", {"files": _j([ref]), "permanent": _j(permanent)})
        return DriveDeleteResult(success=True)

    # Helpers ----------------------------------------------------------------

    async def _list(
        self,
        folder_path: str,
        limit: int,
        offset: int,
        sort_by: SortBy,
        sort_direction: SortDirection,
        file_type: FileType,
        pattern: str | None,
    ) -> _RawFileList:
        params: Params = {
            "path": normalize_drive_path(folder_path),
            "offset": offset,
            "limit": limit,
            "sort_by": sort_by,
            "sort_direction": sort_direction,
        }
        list_filter, local_glob = _list_filter(file_type, pattern)
        if list_filter:
            params["filter"] = json.dumps(list_filter)
        data = await self._dsm.call(FILES_API, "list", FILES_VERSION, params)
        raw = _parse(_RawFileList, data, "list")
        if local_glob is None:
            return raw
        glob = local_glob.lower()
        items = [item for item in raw.items if fnmatch.fnmatchcase(item.name.lower(), glob)]
        return _RawFileList(total=len(items), items=items)

    async def _get_raw(self, ref: str) -> _RawFile:
        data = await self._dsm.call(FILES_API, "get", FILES_VERSION, {"path": ref})
        return _parse(_RawFile, data, "get")

    async def _create_folder(self, path: str) -> _RawFile:
        params: Params = {
            "path": _j(path),
            "type": _j("folder"),
            "conflict_action": _j("autorename"),
        }
        data = await self._dsm.call(FILES_API, "create", FILES_VERSION, params, http_method="POST")
        return _parse(_RawFile, data, "create")

    async def _ensure_folder(self, path: str) -> None:
        """Create each missing folder of `path` below its Drive root."""
        segments = path.strip("/").split("/")
        # `/mydrive` and `/team-folders/NAME` are roots that cannot be created.
        depth = 1 if segments[0] == "mydrive" else 2
        for end in range(depth + 1, len(segments) + 1):
            current = "/" + "/".join(segments[:end])
            try:
                await self._get_raw(current)
            except DsmApiError as error:
                if error.code != FILE_NOT_FOUND:
                    raise
                await self._create_folder(current)

    async def _get_or_create_sharing(self, ref: str) -> _RawSharing:
        try:
            data = await self._dsm.call(
                ADV_SHARING_API, "get", ADV_SHARING_VERSION, {"path": _j(ref)}, http_method="POST"
            )
        except DsmApiError as error:
            if error.code != SHARING_LINK_NOT_CREATED:
                raise
            data = await self._dsm.call(
                ADV_SHARING_API,
                "create",
                ADV_SHARING_VERSION,
                {"path": _j(ref)},
                http_method="POST",
            )
        return _parse(_RawSharing, data, "sharing")

    async def _run_task(self, method: str, params: Params) -> _RawTaskResult:
        data = await self._dsm.call(FILES_API, method, FILES_VERSION, params, http_method="POST")
        task_id = data.get("async_task_id")
        if not isinstance(task_id, str) or not task_id:
            raise DsmNetworkError(f"DSM did not return a task id for {method}", api=FILES_API)
        deadline = _monotonic() + self._task_timeout
        while True:
            data = await self._dsm.call(
                TASKS_API, "get", TASKS_VERSION, {"task_id": _j(task_id)}, http_method="POST"
            )
            task = _parse(_RawTask, data, "task")
            if task.status == "finished":
                if task.result.errors:
                    raise DsmApiError(
                        f"{method} failed: {json.dumps(task.result.errors)[:500]}", api=TASKS_API
                    )
                return task.result
            if task.status not in _TASK_RUNNING:
                raise DsmApiError(f"{method} ended with status {task.status!r}", api=TASKS_API)
            if _monotonic() >= deadline:
                raise DsmNetworkError(
                    f"{method} is still running on the NAS after {self._task_timeout:.0f} s; "
                    "check the result with drive_get_file_info",
                    api=TASKS_API,
                )
            await asyncio.sleep(self._poll_interval)


_FILENAME_STAR = re.compile(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)")
_FILENAME = re.compile(r'filename\s*=\s*(?:"([^"]*)"|([^;]+))')


def _filename(disposition: str) -> str:
    star = _FILENAME_STAR.search(disposition)
    if star:
        return unquote(star[1].strip())
    plain = _FILENAME.search(disposition)
    if plain:
        return (plain[1] if plain[1] is not None else plain[2]).strip()
    return ""
