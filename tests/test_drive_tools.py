from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from syno_workspace_mcp.clients.drive import normalize_drive_path, normalize_path_or_id
from syno_workspace_mcp.config import Settings
from syno_workspace_mcp.errors import InvalidInputError
from syno_workspace_mcp.server import create_server

from .conftest import FakeDsm, load_fixture

READ_TOOLS = {"drive_list_files", "drive_get_file_info", "drive_search_files"}


async def call(mcp: FastMCP, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


# Registration ---------------------------------------------------------------


async def test_read_tools_are_registered_read_only(mcp: FastMCP) -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert tools.keys() >= READ_TOOLS
    for name in READ_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        assert tools[name].description


async def test_drive_tools_absent_when_module_disabled(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.setenv("SYNO_MODULES", "spreadsheet")
    mcp = create_server(Settings())

    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names.isdisjoint(READ_TOOLS)


# drive_list_files -----------------------------------------------------------


async def test_list_team_folder(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=httpx.Response(200, json=load_fixture("drive_list_labs.json")))

    result = await call(mcp, "drive_list_files", {"folder_path": "/team-folders/labs/"})

    assert fake_dsm.entry_params() == {
        "api": "SYNO.SynologyDrive.Files",
        "version": "2",
        "method": "list",
        "path": "/team-folders/labs",
        "offset": "0",
        "limit": "100",
        "sort_by": "name",
        "sort_direction": "ASC",
    }
    assert result["total"] == 2
    assert result["offset"] == 0
    assert result["files"] == [
        {
            "id": "882614125167948800",
            "name": "fichier_labs.osheet",
            "path": "/team-folders/labs/fichier_labs.osheet",
            "type": "file",
            "size": 2048,
            "modified": "2023-11-14T22:13:20.000Z",
            "created": "2023-11-03T08:26:40.000Z",
            "owner": "alice",
            "extension": "osheet",
            "is_shared": True,
        },
        {
            "id": "882614125167948801",
            "name": "archives",
            "path": "/team-folders/labs/archives",
            "type": "dir",
            "size": 0,
            "modified": "2023-11-14T22:13:20.000Z",
            "created": "2023-11-03T08:26:40.000Z",
            "owner": "Bob",
            "extension": "",
            "is_shared": False,
        },
    ]
    assert len(fake_dsm.logouts) == 1  # session closed with the server lifespan


def listed(*names: str, total: int | None = None) -> httpx.Response:
    items = [
        {"file_id": str(i), "name": n, "path": f"/{n}", "type": "file"} for i, n in enumerate(names)
    ]
    data = {"total": len(names) if total is None else total, "items": items}
    return httpx.Response(200, json={"success": True, "data": data})


async def test_list_with_filters(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=listed())

    result = await call(
        mcp,
        "drive_list_files",
        {
            "folder_path": "/mydrive",
            "limit": 5,
            "offset": 10,
            "sort_by": "modified",
            "sort_direction": "DESC",
            "file_type": "dir",
        },
    )

    params = fake_dsm.entry_params()
    # Verified on the NAS: Drive ignores {"type": "dir"}; the type must be a list.
    assert json.loads(params["filter"]) == {"type": ["dir"]}
    assert params["sort_by"] == "modified"
    assert params["limit"] == "5"
    assert result == {"total": 0, "offset": 10, "files": []}


@pytest.mark.parametrize("file_type", ["all", "file"])
async def test_extension_glob_is_filtered_by_drive(
    mcp: FastMCP, fake_dsm: FakeDsm, file_type: str
) -> None:
    fake_dsm.entry.mock(return_value=listed("a.osheet", total=7))

    result = await call(
        mcp, "drive_list_files", {"pattern": "*.OSheet", "file_type": file_type, "limit": 1}
    )

    params = fake_dsm.entry_params()
    # Drive ignores `pattern`; `extensions` alone lets folders through, hence the type.
    assert "pattern" not in params
    assert json.loads(params["filter"]) == {"type": ["file"], "extensions": ["osheet"]}
    assert result["total"] == 7


async def test_other_globs_filter_the_fetched_page(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    page = listed("Q1 Report.pdf", "notes.txt", "report-2.odoc", total=40)
    fake_dsm.entry.mock(return_value=page)

    result = await call(mcp, "drive_list_files", {"pattern": "*report*"})

    params = fake_dsm.entry_params()
    assert "pattern" not in params
    assert "filter" not in params
    assert [f["name"] for f in result["files"]] == ["Q1 Report.pdf", "report-2.odoc"]
    assert result["total"] == 2


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/mydrive/../etc", "'..'"),
        ("/team-folders/labs\\..\\x", "'..'"),
        ("/volume1/homes", "must start with one of"),
        ("/mydrivex", "must start with one of"),
        ("mydrive/labs", "must be absolute"),
        ("   ", "must not be empty"),
    ],
)
async def test_list_rejects_bad_paths_without_calling_dsm(
    mcp: FastMCP, fake_dsm: FakeDsm, path: str, message: str
) -> None:
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=message):
            await client.call_tool("drive_list_files", {"folder_path": path})

    assert fake_dsm.auth.call_count == 0
    assert fake_dsm.entry.call_count == 0


async def test_list_rejects_out_of_range_limit(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("drive_list_files", {"limit": 1001})

    assert fake_dsm.entry.call_count == 0


async def test_dsm_errors_reach_the_client_unmasked(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(
        return_value=httpx.Response(200, json={"success": False, "error": {"code": 408}})
    )

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=r"file or folder not found \(DSM code 408\)"):
            await client.call_tool("drive_list_files", {"folder_path": "/mydrive/missing"})


async def test_unexpected_shape_is_reported(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"items": [{"name": "no id"}]}}
        )
    )

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="unexpected DSM response shape for list"):
            await client.call_tool("drive_list_files", {})


# drive_get_file_info --------------------------------------------------------


async def test_get_file_info(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=httpx.Response(200, json=load_fixture("drive_get_file.json")))

    result = await call(
        mcp, "drive_get_file_info", {"path": "/team-folders/labs/fichier_labs.osheet"}
    )

    params = fake_dsm.entry_params()
    assert params["method"] == "get"
    assert params["path"] == "/team-folders/labs/fichier_labs.osheet"
    assert result["real_path"] == "/volume1/labs/fichier_labs.osheet"
    assert result["labels"] == ["Important", "Draft"]
    assert result["perm"] == {
        "acl": {"append": True, "del": False, "exec": False, "read": True, "write": True},
        "is_owner": False,
    }
    assert result["extension"] == "osheet"


async def test_get_file_info_by_id(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=httpx.Response(200, json=load_fixture("drive_get_file.json")))

    await call(mcp, "drive_get_file_info", {"path": " 882614125167948800 "})

    # DSM addresses files by ID with the `id:` prefix, as the Drive web UI does.
    assert fake_dsm.entry_params()["path"] == "id:882614125167948800"


async def test_get_file_info_defaults_for_sparse_entries(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    sparse = {"file_id": "1", "name": "notes", "path": "/notes", "type": "dir"}
    fake_dsm.entry.mock(return_value=httpx.Response(200, json={"success": True, "data": sparse}))

    result = await call(mcp, "drive_get_file_info", {"path": "/mydrive/notes"})

    assert result["path"] == "/notes"
    assert result["real_path"] == "/notes"
    assert result["owner"] == ""
    assert result["modified"] == ""
    assert result["labels"] == []
    assert result["perm"]["acl"]["read"] is False


# drive_search_files ---------------------------------------------------------


async def test_search(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=httpx.Response(200, json=load_fixture("drive_list_labs.json")))

    result = await call(
        mcp,
        "drive_search_files",
        {"query": "labs", "folder_path": "/team-folders/labs", "extension": "osheet", "limit": 10},
    )

    assert fake_dsm.entry_params() == {
        "api": "SYNO.SynologyDrive.Files",
        "version": "2",
        "method": "search",
        "keyword": "labs",
        "path": "/team-folders/labs",
        "limit": "10",
        "extension": "osheet",
    }
    assert result["offset"] == 0
    assert [f["name"] for f in result["files"]] == ["fichier_labs.osheet", "archives"]


async def test_search_defaults_to_mydrive(mcp: FastMCP, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=httpx.Response(200, json=load_fixture("drive_list_labs.json")))

    await call(mcp, "drive_search_files", {"query": "x"})

    params = fake_dsm.entry_params()
    assert params["path"] == "/mydrive"
    assert params["limit"] == "50"
    assert "extension" not in params


@pytest.mark.parametrize("query", ["", "   "])
async def test_search_rejects_empty_query(mcp: FastMCP, fake_dsm: FakeDsm, query: str) -> None:
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("drive_search_files", {"query": query})

    assert fake_dsm.entry.call_count == 0


# Path guard -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/mydrive", "/mydrive"),
        ("/mydrive/", "/mydrive"),
        ("//mydrive//a/./b/", "/mydrive/a/b"),
        (" /team-folders/labs ", "/team-folders/labs"),
        ("/team-folders/labs/..x", "/team-folders/labs/..x"),
    ],
)
def test_normalize_drive_path(raw: str, expected: str) -> None:
    assert normalize_drive_path(raw) == expected


def test_normalize_path_or_id() -> None:
    assert normalize_path_or_id("123") == "id:123"
    assert normalize_path_or_id("id:123") == "id:123"
    assert normalize_path_or_id("/mydrive/a/") == "/mydrive/a"
    with pytest.raises(InvalidInputError):
        normalize_path_or_id("12a")
