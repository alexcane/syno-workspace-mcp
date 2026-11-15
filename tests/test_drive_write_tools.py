from __future__ import annotations

import base64
import json
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from syno_workspace_mcp.clients import drive as drive_module
from syno_workspace_mcp.clients.drive import validate_name
from syno_workspace_mcp.errors import InvalidInputError
from syno_workspace_mcp.tools import drive as drive_tools

from .conftest import FakeDsm, load_fixture

FILES = "SYNO.SynologyDrive.Files"
TASKS = "SYNO.SynologyDrive.Tasks"
LABELS = "SYNO.SynologyDrive.Labels"
SHARING = "SYNO.SynologyDrive.AdvanceSharing"

WRITE_TOOLS = {
    "drive_get_sharing_link",
    "drive_upload_file",
    "drive_create_folder",
    "drive_add_label",
}
DESTRUCTIVE_TOOLS = {"drive_move_file", "drive_delete_file"}
READ_TOOLS = {"drive_download_file", "drive_list_labels"}

Responder = httpx.Response | Callable[[dict[str, str]], httpx.Response]


def ok(data: dict[str, Any] | None = None) -> httpx.Response:
    payload: dict[str, Any] = {"success": True}
    if data is not None:
        payload["data"] = data
    return httpx.Response(200, json=payload)


def syno_error(code: int) -> httpx.Response:
    return httpx.Response(200, json={"success": False, "error": {"code": code}})


def fixture(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture(name))


def raw_file(
    file_id: str, name: str, display_path: str, parent_id: str, type_: str = "file"
) -> httpx.Response:
    return ok(
        {
            "file_id": file_id,
            "name": name,
            "display_path": display_path,
            "path": display_path,
            "parent_id": parent_id,
            "type": type_,
        }
    )


class DsmScript:
    """Route `entry.cgi` calls by (api, method) to queued responses, and record them."""

    def __init__(self) -> None:
        self.queues: dict[tuple[str, str], list[Responder]] = defaultdict(list)
        self.calls: list[tuple[str, str, dict[str, str], httpx.Request]] = []

    def on(self, api: str, method: str, *responses: Responder) -> DsmScript:
        self.queues[(api, method)].extend(responses)
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        content_type = request.headers.get("content-type", "")
        if request.method == "POST" and content_type.startswith(
            "application/x-www-form-urlencoded"
        ):
            params.update(parse_qsl(request.content.decode()))
        key = (params.get("api", ""), params.get("method", ""))
        self.calls.append((*key, params, request))
        queue = self.queues.get(key)
        if not queue:
            raise AssertionError(f"unexpected DSM call {key}")
        responder = queue.pop(0) if len(queue) > 1 else queue[0]
        return responder(params) if callable(responder) else responder

    def methods(self) -> list[tuple[str, str]]:
        return [(api, method) for api, method, _, _ in self.calls]

    def params(self, api: str, method: str, index: int = 0) -> dict[str, str]:
        found = [p for a, m, p, _ in self.calls if (a, m) == (api, method)]
        return found[index]

    def request(self, api: str, method: str) -> httpx.Request:
        return next(r for a, m, _, r in self.calls if (a, m) == (api, method))


@pytest.fixture
def script(fake_dsm: FakeDsm, monkeypatch: pytest.MonkeyPatch) -> DsmScript:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("syno_workspace_mcp.clients.drive.asyncio.sleep", no_sleep)
    dsm_script = DsmScript()
    fake_dsm.entry.mock(side_effect=dsm_script)
    return dsm_script


async def call(mcp: FastMCP, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


async def call_error(mcp: FastMCP, name: str, arguments: dict[str, Any]) -> str:
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as error:
            await client.call_tool(name, arguments)
    return str(error.value)


# Registration ---------------------------------------------------------------


async def test_all_drive_tools_are_registered_with_annotations(mcp: FastMCP) -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert tools.keys() >= WRITE_TOOLS | DESTRUCTIVE_TOOLS | READ_TOOLS
    for name in READ_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
    for name in WRITE_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
    for name in DESTRUCTIVE_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.destructive_hint is True
        assert tools[name].input_schema["properties"]["confirm"]["default"] is False


# confirm guard ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("drive_move_file", {"path": "/mydrive/a.txt", "dest_folder_path": "/mydrive/b"}),
        (
            "drive_move_file",
            {"path": "/mydrive/a.txt", "dest_folder_path": "/mydrive/b", "confirm": False},
        ),
        ("drive_delete_file", {"path": "/mydrive/a.txt"}),
        ("drive_delete_file", {"path": "/mydrive/a.txt", "permanent": True, "confirm": False}),
    ],
)
async def test_destructive_tools_do_nothing_without_confirm(
    mcp: FastMCP, fake_dsm: FakeDsm, script: DsmScript, tool: str, arguments: dict[str, Any]
) -> None:
    message = await call_error(mcp, tool, arguments)

    assert "requires confirm=true" in message
    assert script.calls == []
    assert fake_dsm.logins == []


# drive_create_folder ----------------------------------------------------------


async def test_create_folder_sends_json_encoded_values(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "create", fixture("drive_create_folder.json"))

    result = await call(
        mcp, "drive_create_folder", {"folder_path": "/team-folders/labs/mcp-test/", "name": "sub"}
    )

    request = script.request(FILES, "create")
    assert request.method == "POST"
    assert script.params(FILES, "create") == {
        "api": FILES,
        "version": "2",
        "method": "create",
        "path": '"/team-folders/labs/mcp-test/sub"',
        "type": '"folder"',
        "conflict_action": '"autorename"',
    }
    assert result == {
        "success": True,
        "folder_id": "900000000000000002",
        "folder_path": "/team-folders/labs/mcp-test/sub",
    }


async def test_create_folder_with_force_parent_creates_missing_ancestors(
    mcp: FastMCP, script: DsmScript
) -> None:
    script.on(
        FILES,
        "get",
        raw_file("1", "a", "/team-folders/labs/a", "0", "dir"),
        syno_error(1003),
    )
    script.on(FILES, "create", fixture("drive_create_folder.json"))

    await call(
        mcp,
        "drive_create_folder",
        {"folder_path": "/team-folders/labs/a/b", "name": "c", "force_parent": True},
    )

    # The team folder root is never created; `a` exists; `b` then `c` are created.
    assert script.methods() == [
        (FILES, "get"),
        (FILES, "get"),
        (FILES, "create"),
        (FILES, "create"),
    ]
    assert script.params(FILES, "get", 0)["path"] == "/team-folders/labs/a"
    assert script.params(FILES, "create", 0)["path"] == '"/team-folders/labs/a/b"'
    assert script.params(FILES, "create", 1)["path"] == '"/team-folders/labs/a/b/c"'


async def test_force_parent_propagates_other_errors(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "get", syno_error(105))

    message = await call_error(
        mcp,
        "drive_create_folder",
        {"folder_path": "/mydrive/a", "name": "b", "force_parent": True},
    )

    assert "permission denied" in message
    assert (FILES, "create") not in script.methods()


@pytest.mark.parametrize("name", ["", " ", "a/b", "a\\b", ".", ".."])
async def test_create_folder_rejects_bad_names(mcp: FastMCP, script: DsmScript, name: str) -> None:
    message = await call_error(
        mcp, "drive_create_folder", {"folder_path": "/mydrive", "name": name}
    )

    assert "name" in message
    assert script.calls == []


def test_validate_name_strips_spaces() -> None:
    assert validate_name("  report.pdf ") == "report.pdf"
    with pytest.raises(InvalidInputError):
        validate_name("../x")


# drive_upload_file ------------------------------------------------------------


async def test_upload_sends_multipart_with_query_api(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "upload", fixture("drive_upload.json"))
    content = b"hello mcp"

    result = await call(
        mcp,
        "drive_upload_file",
        {
            "dest_folder_path": "/team-folders/labs/mcp-test",
            "file_name": "notes.txt",
            "content_base64": base64.b64encode(content).decode(),
            "mime_type": "text/plain",
        },
    )

    request = script.request(FILES, "upload")
    assert request.method == "POST"
    assert dict(request.url.params) == {"api": FILES, "version": "2", "method": "upload"}
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="path"\r\n\r\n/team-folders/labs/mcp-test/notes.txt\r\n' in body
    assert b'name="type"\r\n\r\nfile\r\n' in body
    assert b'name="conflict_action"\r\n\r\nversion\r\n' in body
    assert b'filename="notes.txt"' in body
    assert b"Content-Type: text/plain\r\n\r\nhello mcp\r\n" in body
    assert result == {
        "success": True,
        "file_id": "900000000000000003",
        "file_path": "/team-folders/labs/mcp-test/notes.txt",
        "file_name": "notes.txt",
    }


async def test_upload_rejects_invalid_base64(mcp: FastMCP, script: DsmScript) -> None:
    message = await call_error(
        mcp,
        "drive_upload_file",
        {"dest_folder_path": "/mydrive", "file_name": "a.txt", "content_base64": "not base64!"},
    )

    assert "not valid base64" in message
    assert script.calls == []


async def test_upload_rejects_oversized_payload(
    mcp: FastMCP, script: DsmScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drive_tools, "MAX_TRANSFER_BYTES", 3)

    message = await call_error(
        mcp,
        "drive_upload_file",
        {
            "dest_folder_path": "/mydrive",
            "file_name": "a.txt",
            "content_base64": base64.b64encode(b"0123456789").decode(),
        },
    )

    assert "limit" in message
    assert script.calls == []


async def test_upload_rejects_path_outside_drive(mcp: FastMCP, script: DsmScript) -> None:
    message = await call_error(
        mcp,
        "drive_upload_file",
        {"dest_folder_path": "/volume1/etc", "file_name": "a.txt", "content_base64": "YQ=="},
    )

    assert "must start with" in message
    assert script.calls == []


# drive_download_file ----------------------------------------------------------


async def test_download_returns_base64_content(mcp: FastMCP, script: DsmScript) -> None:
    script.on(
        FILES,
        "download",
        httpx.Response(
            200,
            content=b"hello mcp",
            headers={
                "content-type": "text/plain; charset=utf-8",
                "content-disposition": "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.txt",
            },
        ),
    )

    result = await call(mcp, "drive_download_file", {"path": "900000000000000003"})

    request = script.request(FILES, "download")
    assert request.method == "GET"
    assert script.params(FILES, "download") == {
        "api": FILES,
        "version": "2",
        "method": "download",
        "files": '["id:900000000000000003"]',
        "force_download": "true",
        "json_error": "true",
        "download_type": '"download"',
    }
    assert result == {
        "file_name": "résumé.txt",
        "mime_type": "text/plain",
        "size": 9,
        "content_base64": base64.b64encode(b"hello mcp").decode(),
    }


async def test_download_falls_back_to_path_name(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "download", httpx.Response(200, content=b"x"))

    result = await call(mcp, "drive_download_file", {"path": "/mydrive/docs/a.bin"})

    assert result["file_name"] == "a.bin"
    assert result["mime_type"] == "application/octet-stream"


async def test_download_reports_json_error_envelope(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "download", syno_error(1003))

    message = await call_error(mcp, "drive_download_file", {"path": "/mydrive/missing.txt"})

    assert "file or folder not found" in message


async def test_download_refuses_files_over_the_limit(
    mcp: FastMCP, script: DsmScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drive_module, "MAX_TRANSFER_BYTES", 4)
    script.on(FILES, "download", httpx.Response(200, content=b"0123456789"))

    message = await call_error(mcp, "drive_download_file", {"path": "/mydrive/big.bin"})

    assert "limit" in message


# Labels -----------------------------------------------------------------------


async def test_list_labels(mcp: FastMCP, script: DsmScript) -> None:
    script.on(LABELS, "list", fixture("drive_labels.json"))

    result = await call(mcp, "drive_list_labels", {})

    assert script.params(LABELS, "list") == {"api": LABELS, "version": "1", "method": "list"}
    assert result == {"labels": [{"id": "2", "name": "urgent", "color": "#DCE1E6"}]}


async def test_add_label_resolves_name_to_id(mcp: FastMCP, script: DsmScript) -> None:
    script.on(LABELS, "list", fixture("drive_labels.json"))
    script.on(FILES, "label", ok())

    result = await call(
        mcp, "drive_add_label", {"path": "/team-folders/labs/a.txt", "label_name": "Urgent"}
    )

    params = script.params(FILES, "label")
    assert json.loads(params["files"]) == ["/team-folders/labs/a.txt"]
    assert json.loads(params["labels"]) == [{"action": "add", "label_id": "2"}]
    assert result == {"success": True}


async def test_add_unknown_label_lists_available_ones(mcp: FastMCP, script: DsmScript) -> None:
    script.on(LABELS, "list", fixture("drive_labels.json"))

    message = await call_error(
        mcp, "drive_add_label", {"path": "/mydrive/a.txt", "label_name": "missing"}
    )

    assert "does not exist" in message
    assert "urgent" in message
    assert (FILES, "label") not in script.methods()


# drive_get_sharing_link -------------------------------------------------------


async def test_sharing_link_created_with_password_and_expiry(
    mcp: FastMCP,
    script: DsmScript,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("syno_workspace_mcp.clients.drive.time.time", lambda: 1_700_000_000)
    script.on(SHARING, "get", syno_error(1036))
    script.on(SHARING, "create", fixture("drive_sharing_create.json"))
    script.on(SHARING, "update", ok())
    caplog.set_level(logging.DEBUG)

    result = await call(
        mcp,
        "drive_get_sharing_link",
        {
            "path": "/team-folders/labs/a.txt",
            "permission": "download",
            "password": "link-secret-42",
            "expire_days": 2,
        },
    )

    assert script.methods() == [(SHARING, "get"), (SHARING, "create"), (SHARING, "update")]
    assert script.params(SHARING, "create")["path"] == '"/team-folders/labs/a.txt"'
    update = script.params(SHARING, "update")
    assert update["sharing_link"] == '"LINK-fixture-0001"'
    assert update["role"] == '"viewer"'
    assert update["protect_password"] == '"link-secret-42"'
    assert update["due_date"] == str(1_700_000_000 + 2 * 86400)
    assert result == {
        "link": "https://nas.example.test/d/s/PERMALINK-fixture/LINK-fixture-0001",
        "permission": "download",
        "expires_at": "2023-11-16T22:13:20.000Z",
    }
    assert "link-secret-42" not in caplog.text


async def test_sharing_link_reuses_existing_link_and_clears_protection(
    mcp: FastMCP, script: DsmScript
) -> None:
    script.on(SHARING, "get", fixture("drive_sharing_create.json"))
    script.on(SHARING, "update", ok())

    result = await call(mcp, "drive_get_sharing_link", {"path": "900000000000000003"})

    assert (SHARING, "create") not in script.methods()
    update = script.params(SHARING, "update")
    assert update["path"] == '"id:900000000000000003"'
    assert update["role"] == '"previewer"'
    assert update["protect_password"] == '""'
    assert update["due_date"] == "0"
    assert result["expires_at"] is None


async def test_sharing_link_propagates_other_errors(mcp: FastMCP, script: DsmScript) -> None:
    script.on(SHARING, "get", syno_error(105))

    message = await call_error(mcp, "drive_get_sharing_link", {"path": "/mydrive/a.txt"})

    assert "permission denied" in message
    assert (SHARING, "create") not in script.methods()


# drive_move_file --------------------------------------------------------------

SOURCE = raw_file("900000000000000003", "notes.txt", "/team-folders/labs/mcp-test/notes.txt", "1")
DEST = raw_file("900000000000000002", "sub", "/team-folders/labs/mcp-test/sub", "1", "dir")
MOVED = raw_file(
    "900000000000000003", "notes.txt", "/team-folders/labs/mcp-test/sub/notes.txt", "2"
)
IN_PROGRESS = ok({"progress": 0, "status": "in_progress", "task_id": "task-1"})


async def test_move_runs_the_task_until_finished(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "get", SOURCE, DEST, MOVED)
    script.on(FILES, "move", ok({"async_task_id": "task-1"}))
    script.on(TASKS, "get", IN_PROGRESS, fixture("drive_task_move_finished.json"))

    result = await call(
        mcp,
        "drive_move_file",
        {
            "path": "/team-folders/labs/mcp-test/notes.txt",
            "dest_folder_path": "/team-folders/labs/mcp-test/sub",
            "confirm": True,
        },
    )

    assert script.params(FILES, "move") == {
        "api": FILES,
        "version": "2",
        "method": "move",
        "to_parent_folder": '"id:900000000000000002"',
        "files": '["id:900000000000000003"]',
        "conflict_action": '"autorename"',
    }
    assert script.params(TASKS, "get")["task_id"] == '"task-1"'
    assert script.methods().count((TASKS, "get")) == 2
    assert result == {
        "dry_run": False,
        "success": True,
        "new_path": "/team-folders/labs/mcp-test/sub/notes.txt",
    }


async def test_move_with_rename(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "get", SOURCE, DEST)
    script.on(FILES, "move", ok({"async_task_id": "task-1"}))
    script.on(TASKS, "get", fixture("drive_task_move_finished.json"))
    script.on(FILES, "update", fixture("drive_rename.json"))

    result = await call(
        mcp,
        "drive_move_file",
        {
            "path": "900000000000000003",
            "dest_folder_path": "900000000000000002",
            "new_name": "renamed.txt",
            "conflict_action": "version",
            "confirm": True,
        },
    )

    assert script.params(FILES, "move")["conflict_action"] == '"version"'
    assert script.params(FILES, "update") == {
        "api": FILES,
        "version": "2",
        "method": "update",
        "path": '"id:900000000000000003"',
        "name": '"renamed.txt"',
    }
    assert result["new_path"] == "/team-folders/labs/mcp-test/sub/renamed.txt"


async def test_rename_in_place_skips_the_move(mcp: FastMCP, script: DsmScript) -> None:
    parent = raw_file("1", "mcp-test", "/team-folders/labs/mcp-test", "0", "dir")
    script.on(FILES, "get", SOURCE, parent)
    script.on(FILES, "update", fixture("drive_rename.json"))

    await call(
        mcp,
        "drive_move_file",
        {
            "path": "/team-folders/labs/mcp-test/notes.txt",
            "dest_folder_path": "/team-folders/labs/mcp-test",
            "new_name": "renamed.txt",
            "confirm": True,
        },
    )

    assert (FILES, "move") not in script.methods()
    assert (FILES, "update") in script.methods()


async def test_move_to_same_folder_without_rename_is_refused(
    mcp: FastMCP, script: DsmScript
) -> None:
    parent = raw_file("1", "mcp-test", "/team-folders/labs/mcp-test", "0", "dir")
    script.on(FILES, "get", SOURCE, parent)

    message = await call_error(
        mcp,
        "drive_move_file",
        {
            "path": "/team-folders/labs/mcp-test/notes.txt",
            "dest_folder_path": "/team-folders/labs/mcp-test",
            "confirm": True,
        },
    )

    assert "nothing to do" in message


async def test_move_into_a_file_is_refused(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "get", SOURCE, SOURCE)

    message = await call_error(
        mcp,
        "drive_move_file",
        {"path": "/mydrive/a.txt", "dest_folder_path": "/mydrive/b.txt", "confirm": True},
    )

    assert "must be a folder" in message
    assert (FILES, "move") not in script.methods()


async def test_move_reports_task_errors(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "get", SOURCE, DEST)
    script.on(FILES, "move", ok({"async_task_id": "task-1"}))
    script.on(
        TASKS,
        "get",
        ok({"status": "finished", "result": {"errors": [{"code": 1002, "path": "notes.txt"}]}}),
    )

    message = await call_error(
        mcp,
        "drive_move_file",
        {"path": "/mydrive/a.txt", "dest_folder_path": "/mydrive/b", "confirm": True},
    )

    assert "move failed" in message
    assert "1002" in message


# drive_delete_file ------------------------------------------------------------


@pytest.mark.parametrize(("permanent", "encoded"), [(False, "false"), (True, "true")])
async def test_delete_runs_the_task(
    mcp: FastMCP, script: DsmScript, permanent: bool, encoded: str
) -> None:
    script.on(FILES, "delete", ok({"async_task_id": "task-2"}))
    script.on(
        TASKS,
        "get",
        ok({"progress": 0, "status": "in_progress", "result": {"errors": None}}),
        ok({"progress": 100, "status": "finished", "result": {"action": "delete", "errors": None}}),
    )

    result = await call(
        mcp,
        "drive_delete_file",
        {"path": "/team-folders/labs/mcp-test/a.txt", "permanent": permanent, "confirm": True},
    )

    assert script.params(FILES, "delete") == {
        "api": FILES,
        "version": "2",
        "method": "delete",
        "files": '["/team-folders/labs/mcp-test/a.txt"]',
        "permanent": encoded,
    }
    assert result == {"success": True}


async def test_delete_reports_unexpected_task_status(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "delete", ok({"async_task_id": "task-2"}))
    script.on(TASKS, "get", ok({"status": "failed"}))

    message = await call_error(
        mcp, "drive_delete_file", {"path": "/mydrive/a.txt", "confirm": True}
    )

    assert "status 'failed'" in message


async def test_delete_without_task_id_is_an_error(mcp: FastMCP, script: DsmScript) -> None:
    script.on(FILES, "delete", ok({}))

    message = await call_error(
        mcp, "drive_delete_file", {"path": "/mydrive/a.txt", "confirm": True}
    )

    assert "task id" in message


async def test_task_timeout_says_the_task_may_still_run(
    mcp: FastMCP, script: DsmScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter([0.0, 500.0])
    monkeypatch.setattr(drive_module, "_monotonic", lambda: next(clock))
    script.on(FILES, "delete", ok({"async_task_id": "task-2"}))
    script.on(TASKS, "get", IN_PROGRESS)

    message = await call_error(
        mcp, "drive_delete_file", {"path": "/mydrive/a.txt", "confirm": True}
    )

    assert "still running" in message
