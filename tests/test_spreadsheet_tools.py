from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from syno_workspace_mcp.config import Settings
from syno_workspace_mcp.server import create_server
from syno_workspace_mcp.store import SpreadsheetRegistry

from .conftest import FakeDsm, FakeSpreadsheetApi, json_body, load_fixture

SID = "AbCdEf1234567890GhIjKl"
BASE = f"/spreadsheets/{SID}"

READ_TOOLS = {
    "spreadsheet_list",
    "spreadsheet_get_info",
    "spreadsheet_read_sheet",
    "spreadsheet_get_styles",
    "spreadsheet_export",
}
DESTRUCTIVE_TOOLS = {
    "spreadsheet_write_cells",
    "spreadsheet_write_styles",
    "spreadsheet_batch_update",
    "spreadsheet_delete_sheet",
    "spreadsheet_delete_file",
}
WRITE_TOOLS = {
    "spreadsheet_register",
    "spreadsheet_create",
    "spreadsheet_add_sheet",
    "spreadsheet_rename_sheet",
    "spreadsheet_append_rows",
}
ALL_TOOLS = READ_TOOLS | DESTRUCTIVE_TOOLS | WRITE_TOOLS

# Minimal valid arguments for each confirm-guarded tool (confirm left out).
CONFIRM_CALLS: dict[str, dict[str, Any]] = {
    "spreadsheet_write_cells": {"sheet_name": "Sheet1", "start_cell": "A1", "values": [["x"]]},
    "spreadsheet_append_rows": {"sheet_name": "Sheet1", "rows": [["x"]]},
    "spreadsheet_write_styles": {
        "sheet_name": "Sheet1",
        "start_row": 0,
        "start_col": 0,
        "styles": [[{"bg": "ff0000"}]],
    },
    "spreadsheet_batch_update": {
        "sheet_id": "sh_1",
        "action": "delete_rows",
        "index": 0,
        "count": 1,
    },
    "spreadsheet_rename_sheet": {"sheet_id": "sh_1", "new_name": "Renamed"},
    "spreadsheet_delete_sheet": {"sheet_id": "sh_1"},
    "spreadsheet_delete_file": {},
}


def ok(payload: Any = None) -> httpx.Response:
    return httpx.Response(200, json=payload if payload is not None else {})


def fixture(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture(name))


async def call(mcp: FastMCP, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


async def call_error(mcp: FastMCP, name: str, arguments: dict[str, Any]) -> str:
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(name, arguments)
    return str(caught.value)


@pytest.fixture
def registry(settings: Settings) -> SpreadsheetRegistry:
    return SpreadsheetRegistry(settings.data_dir)


# Registration ---------------------------------------------------------------


async def test_all_spreadsheet_tools_are_registered_with_annotations(mcp: FastMCP) -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert tools.keys() >= ALL_TOOLS
    for name in ALL_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None, name
        assert annotations.read_only_hint is (name in READ_TOOLS), name
        assert annotations.destructive_hint is (name in DESTRUCTIVE_TOOLS), name
        assert tools[name].description, name
    for name in CONFIRM_CALLS:
        assert "confirm" in tools[name].input_schema["properties"], name


async def test_spreadsheet_tools_absent_when_module_disabled(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.setenv("SYNO_MODULES", "drive")
    mcp = create_server(Settings())

    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names.isdisjoint(ALL_TOOLS)
    assert "drive_list_files" in names


# confirm ----------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(CONFIRM_CALLS))
@pytest.mark.parametrize("confirm", [None, False])
async def test_confirm_guard_blocks_before_any_call(
    mcp: FastMCP,
    fake_ss: FakeSpreadsheetApi,
    registry: SpreadsheetRegistry,
    tool: str,
    confirm: bool | None,
) -> None:
    registry.register("labs", SID, path=None, source="manual")
    arguments = {**CONFIRM_CALLS[tool], "name": "labs"}
    if confirm is not None:
        arguments["confirm"] = confirm

    message = await call_error(mcp, tool, arguments)

    assert "confirm=true" in message
    assert fake_ss.calls == []
    assert fake_ss.authorizations == []
    assert registry.resolve("labs") == SID


# Addressing -------------------------------------------------------------------


async def test_file_id_accepts_office_url(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("GET", BASE, fixture("ss_get_info.json"))

    result = await call(
        mcp,
        "spreadsheet_get_info",
        {"file_id": f"https://drive.example.test/oo/r/{SID}#tid=1"},
    )

    assert result["name"] == "fichier_labs"
    assert [s["sheet_id"] for s in result["sheets"]] == ["sh_2", "sh_1"]


async def test_name_resolves_through_registry(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi, registry: SpreadsheetRegistry
) -> None:
    registry.register("labs", SID, path="/team-folders/labs/fichier_labs.osheet", source="manual")
    fake_ss.on("GET", BASE, fixture("ss_get_info.json"))

    result = await call(
        mcp,
        "spreadsheet_get_info",
        {"name": "labs", "path": "/team-folders/labs//fichier_labs.osheet"},
    )

    assert result["file_id"] == SID


async def test_unknown_name_is_a_clear_error(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    message = await call_error(mcp, "spreadsheet_get_info", {"name": "nope"})

    assert "spreadsheet_register" in message
    assert fake_ss.calls == []


async def test_no_identifier(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    assert "file_id, name or path" in await call_error(mcp, "spreadsheet_get_info", {})


def drive_file(name: str, permanent_link: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "file_id": "977513347547182752",
        "name": name,
        "path": f"/{name}",
        "display_path": f"/team-folders/labs/{name}",
        "type": "file",
        "size": 940,
        "owner": {"name": "mcp_service"},
    }
    if permanent_link is not None:
        record["permanent_link"] = permanent_link
    return record


async def test_drive_path_alone_resolves_through_permanent_link(
    mcp: FastMCP, fake_dsm: FakeDsm, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_dsm.entry.mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": drive_file("fichier_labs.osheet", SID)}
        )
    )
    fake_ss.on("GET", BASE, fixture("ss_get_info.json"))

    result = await call(
        mcp, "spreadsheet_get_info", {"path": "/team-folders/labs/fichier_labs.osheet"}
    )

    assert fake_dsm.entry_params()["method"] == "get"
    assert fake_dsm.entry_params()["path"] == "/team-folders/labs/fichier_labs.osheet"
    assert result["file_id"] == SID


async def test_drive_path_of_a_non_spreadsheet_is_rejected(
    mcp: FastMCP, fake_dsm: FakeDsm, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_dsm.entry.mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": drive_file("notes.pdf", "PermaLink12345678")}
        )
    )

    message = await call_error(
        mcp, "spreadsheet_get_info", {"path": "/team-folders/labs/notes.pdf"}
    )

    assert "not a Synology Spreadsheet" in message
    assert fake_ss.calls == []


# Read -------------------------------------------------------------------------


async def test_list_takes_spreadsheet_id_from_permanent_link(
    mcp: FastMCP, fake_dsm: FakeDsm, fake_ss: FakeSpreadsheetApi
) -> None:
    listing = {
        "total": 2,
        "items": [drive_file("fichier_labs.osheet", SID), drive_file("other.osheet", None)],
    }
    fake_dsm.entry.mock(return_value=httpx.Response(200, json={"success": True, "data": listing}))

    result = await call(mcp, "spreadsheet_list", {"folder_path": "/team-folders/labs"})

    assert [f["spreadsheet_id"] for f in result["files"]] == [SID, None]
    assert result["files"][0]["file_id"] == "977513347547182752"


async def test_list_falls_back_to_registry_without_permanent_link(
    mcp: FastMCP, fake_dsm: FakeDsm, fake_ss: FakeSpreadsheetApi, registry: SpreadsheetRegistry
) -> None:
    fake_dsm.entry.mock(return_value=httpx.Response(200, json=load_fixture("drive_list_labs.json")))
    registry.register("labs", SID, path="/team-folders/labs/fichier_labs.osheet", source="manual")

    result = await call(mcp, "spreadsheet_list", {"folder_path": "/team-folders/labs"})

    params = fake_dsm.entry_params()
    assert "pattern" not in params
    assert json.loads(params["filter"]) == {"type": ["file"], "extensions": ["osheet"]}
    assert params["limit"] == "50"
    osheets = [f for f in result["files"] if f["path"].endswith(".osheet")]
    assert osheets
    assert all(f["path"].endswith(".osheet") for f in result["files"])
    assert {f["spreadsheet_id"] for f in osheets} == {SID}


async def test_read_sheet_defaults_to_first_sheet_and_used_area(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("GET", BASE, fixture("ss_get_info.json"))
    fake_ss.on("GET", f"{BASE}/values/Sheet1%21A1%3AC3", fixture("ss_values.json"))

    result = await call(mcp, "spreadsheet_read_sheet", {"file_id": SID})

    assert result == {
        "file_id": SID,
        "sheet_name": "Sheet1",
        # The used area, not the grid that was read.
        "range": "A1:C2",
        "headers": ["Name", "Age", "City"],
        "rows": [["Alice", 30, "Paris"]],
        "total_rows": 1,
        "total_cols": 3,
    }


async def test_read_sheet_with_explicit_range_skips_info_and_keeps_shape(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("GET", f"{BASE}/values/Sheet1%21A1%3AC3", fixture("ss_values.json"))

    result = await call(
        mcp, "spreadsheet_read_sheet", {"file_id": SID, "sheet_name": "Sheet1", "range": "A1:C3"}
    )

    assert len(fake_ss.calls) == 1
    assert result["rows"] == [["Alice", 30, "Paris"], ["", None, ""]]


async def test_read_sheet_keeps_empty_header_cells_as_null(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    values = {"range": "A1:D2", "values": [["Name", None, "", "City"], ["a", 1, 2, "b"]]}
    fake_ss.on("GET", f"{BASE}/values/Sheet1%21A1%3AD2", httpx.Response(200, json=values))

    result = await call(
        mcp, "spreadsheet_read_sheet", {"file_id": SID, "sheet_name": "Sheet1", "range": "A1:D2"}
    )

    # Same convention as the rows: an empty cell is null, an empty string stays "".
    assert result["headers"] == ["Name", None, "", "City"]


async def test_read_sheet_of_an_empty_sheet(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("GET", BASE, fixture("ss_get_info.json"))
    fake_ss.on(
        "GET", f"{BASE}/values/Sheet1%21A1%3AC3", httpx.Response(200, json={"values": [[], []]})
    )

    result = await call(mcp, "spreadsheet_read_sheet", {"file_id": SID})

    assert result["range"] == "A1"
    assert result["headers"] == []
    assert result["rows"] == []


async def test_read_sheet_unknown_sheet_lists_names(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("GET", BASE, fixture("ss_get_info.json"))

    message = await call_error(mcp, "spreadsheet_read_sheet", {"file_id": SID, "sheet_name": "X"})

    assert "'Notes'" in message
    assert "'Sheet1'" in message


FORMULA_VALUES = {
    "range": "A1:C2",
    "values": [["Name", "Qty", "Total"], ["a", 2, "=B2*10"]],
}


def formula_styles() -> dict[str, Any]:
    def cell(entered: Any, effective: Any) -> dict[str, Any]:
        return {"userEnteredValue": entered, "effectiveValue": effective}

    return {
        "range": "A1:C2",
        "rows": [
            {"values": [cell("Name", "Name"), cell("Qty", "Qty"), cell("Total", "Total")]},
            {"values": [cell("a", "a"), cell(2, 2), cell("=B2*10", 20)]},
        ],
    }


async def test_read_sheet_returns_formula_results_by_default(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    # Observed on the NAS: the values endpoint returns formulas as typed.
    fake_ss.on("GET", f"{BASE}/values/Sheet1%21A1%3AE10", ok(FORMULA_VALUES))
    fake_ss.on("GET", f"{BASE}/styles/Sheet1%21A1%3AC2", ok(formula_styles()))

    result = await call(
        mcp, "spreadsheet_read_sheet", {"file_id": SID, "sheet_name": "Sheet1", "range": "A1:E10"}
    )

    assert result["rows"] == [["a", 2, 20]]


async def test_read_sheet_with_formulas_keeps_them_as_typed(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("GET", f"{BASE}/values/Sheet1%21A1%3AE10", ok(FORMULA_VALUES))

    result = await call(
        mcp,
        "spreadsheet_read_sheet",
        {"file_id": SID, "sheet_name": "Sheet1", "range": "A1:E10", "include_formulas": True},
    )

    assert result["rows"] == [["a", 2, "=B2*10"]]
    assert len(fake_ss.calls) == 1


async def test_read_sheet_without_formulas_skips_styles(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("GET", f"{BASE}/values/Sheet1%21A1%3AC3", fixture("ss_values.json"))

    await call(
        mcp, "spreadsheet_read_sheet", {"file_id": SID, "sheet_name": "Sheet1", "range": "A1:C3"}
    )

    assert len(fake_ss.calls) == 1


async def test_formula_error_is_returned_as_its_code(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    styles = formula_styles()
    styles["rows"][1]["values"][2]["effectiveValue"] = {"error": "#DIV/0!"}
    fake_ss.on("GET", f"{BASE}/values/Sheet1%21A1%3AE10", ok(FORMULA_VALUES))
    fake_ss.on("GET", f"{BASE}/styles/Sheet1%21A1%3AC2", ok(styles))

    result = await call(
        mcp, "spreadsheet_read_sheet", {"file_id": SID, "sheet_name": "Sheet1", "range": "A1:E10"}
    )

    assert result["rows"] == [["a", 2, "#DIV/0!"]]


async def test_get_styles(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("GET", f"{BASE}/styles/Sheet1%21A1%3AB1", fixture("ss_styles.json"))

    result = await call(
        mcp, "spreadsheet_get_styles", {"file_id": SID, "sheet_name": "Sheet1", "range": "A1:B1"}
    )

    assert result["range"] == "Sheet1!A1:B1"
    assert result["styles"][0][0]["userEnteredFormat"] == {"textFormat": {"bold": True}}


async def test_export_csv_targets_the_named_sheet(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("GET", BASE, fixture("ss_get_info.json"))
    fake_ss.on(
        "GET",
        f"{BASE}/sheet/csv",
        httpx.Response(
            200,
            content=b"n,1\n",
            headers={"content-type": "text/csv", "content-disposition": 'filename="Notes.csv"'},
        ),
    )

    result = await call(
        mcp, "spreadsheet_export", {"file_id": SID, "format": "csv", "sheet_name": "Notes"}
    )

    assert fake_ss.call().url.params["sheetId"] == "sh_2"
    assert result["file_name"] == "Notes.csv"
    assert base64.b64decode(result["content_base64"]) == b"n,1\n"
    assert result["size"] == 4


async def test_export_xlsx_does_not_need_info(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("GET", f"{BASE}/xlsx", httpx.Response(200, content=b"PK\x03\x04"))

    result = await call(mcp, "spreadsheet_export", {"file_id": SID, "format": "xlsx"})

    assert len(fake_ss.calls) == 1
    assert result["size"] == 4


# Registry tools ---------------------------------------------------------------


async def test_register_checks_the_id_then_stores_it(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi, registry: SpreadsheetRegistry
) -> None:
    fake_ss.on("GET", BASE, fixture("ss_get_info.json"))

    result = await call(
        mcp,
        "spreadsheet_register",
        {
            "name": "fichier_labs",
            "spreadsheet_id": f"https://drive.example.test/oo/r/{SID}",
            "path": "/team-folders/labs/fichier_labs.osheet",
        },
    )

    assert result["spreadsheet_name"] == "fichier_labs"
    assert result["entry"]["spreadsheet_id"] == SID
    assert registry.resolve("fichier_labs") == SID


async def test_register_unknown_id_stores_nothing(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi, registry: SpreadsheetRegistry
) -> None:
    fake_ss.on("GET", BASE, httpx.Response(404))

    message = await call_error(mcp, "spreadsheet_register", {"name": "labs", "spreadsheet_id": SID})

    assert "not found" in message
    assert registry.list() == []


async def test_register_rejects_path_outside_drive(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    message = await call_error(
        mcp,
        "spreadsheet_register",
        {"name": "labs", "spreadsheet_id": SID, "path": "/volume1/../etc"},
    )

    assert ".." in message
    assert fake_ss.calls == []


# Write ------------------------------------------------------------------------


async def test_create_registers_the_new_file(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi, registry: SpreadsheetRegistry
) -> None:
    fake_ss.on("POST", "/spreadsheets/create", ok({"spreadsheetId": SID}))

    result = await call(mcp, "spreadsheet_create", {"name": "Budget"})

    assert json_body(fake_ss.call()) == {"name": "Budget"}
    assert result == {"success": True, "file_id": SID, "name": "Budget"}
    assert registry.resolve("Budget") == SID


async def test_add_sheet(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("POST", f"{BASE}/sheet/add", fixture("ss_add_sheet.json"))

    result = await call(mcp, "spreadsheet_add_sheet", {"file_id": SID, "sheet_name": "Budget"})

    assert json_body(fake_ss.call()) == {"sheetName": "Budget"}
    assert result == {"success": True, "sheet_id": "sh_3", "index": 2}


async def test_rename_sheet(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("POST", f"{BASE}/sheet/rename", ok({"spreadsheetId": SID}))

    result = await call(
        mcp,
        "spreadsheet_rename_sheet",
        {"file_id": SID, "sheet_id": "sh_1", "new_name": "Data", "confirm": True},
    )

    assert json_body(fake_ss.call()) == {"sheetId": "sh_1", "sheetName": "Data"}
    assert result["new_name"] == "Data"


async def test_write_cells(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("PUT", f"{BASE}/values/Sheet1%21B2%3AC3", fixture("ss_values.json"))

    result = await call(
        mcp,
        "spreadsheet_write_cells",
        {
            "file_id": SID,
            "sheet_name": "Sheet1",
            "start_cell": "B2",
            "values": [["Name", "Age"], ["Bob", 41.5]],
            "confirm": True,
        },
    )

    assert json_body(fake_ss.call()) == {"values": [["Name", "Age"], ["Bob", 41.5]]}
    assert result == {"success": True, "cells_written": 4, "range_written": "B2:C3"}


async def test_write_cells_invalid_start_cell(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    message = await call_error(
        mcp,
        "spreadsheet_write_cells",
        {"file_id": SID, "sheet_name": "S", "start_cell": "1A", "values": [["x"]], "confirm": True},
    )

    assert "A1 notation" in message
    assert fake_ss.calls == []


async def test_append_rows(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("PUT", f"{BASE}/values/Sheet1%21A1%3AC2/append", fixture("ss_append.json"))

    result = await call(
        mcp,
        "spreadsheet_append_rows",
        {
            "file_id": SID,
            "sheet_name": "Sheet1",
            "rows": [["a", "b", "c"], ["d"]],
            "confirm": True,
        },
    )

    assert result == {"success": True, "rows_appended": 2, "range_written": "A4:C5"}


async def test_write_styles_sends_offsets_and_only_given_fields(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("PUT", f"{BASE}/styles", ok())

    result = await call(
        mcp,
        "spreadsheet_write_styles",
        {
            "file_id": SID,
            "sheet_name": "Sheet1",
            "start_row": 1,
            "start_col": 2,
            "styles": [
                [{"textFormat": {"bold": True}}, {"bg": None}],
                [{"borders": ["ff0000", None, None, None]}, {"horizontalAlignment": "center"}],
            ],
            "confirm": True,
        },
    )

    assert json_body(fake_ss.call()) == {
        "sheetName": "Sheet1",
        "startRow": 1,
        "startCol": 2,
        "rows": [
            {
                "values": [
                    {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    {"userEnteredFormat": {"bg": None}},
                ]
            },
            {
                "values": [
                    {"userEnteredFormat": {"borders": ["ff0000", None, None, None]}},
                    {"userEnteredFormat": {"horizontalAlignment": "center"}},
                ]
            },
        ],
    }
    assert result["range_written"] == "C2:D3"


async def test_write_styles_rejects_ragged_grid(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    message = await call_error(
        mcp,
        "spreadsheet_write_styles",
        {
            "file_id": SID,
            "sheet_name": "Sheet1",
            "start_row": 0,
            "start_col": 0,
            "styles": [[{"bg": "ff0000"}], [{"bg": "ff0000"}, {"bg": "00ff00"}]],
            "confirm": True,
        },
    )

    assert "rectangular" in message
    assert fake_ss.calls == []


async def test_write_styles_rejects_unknown_attribute(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi
) -> None:
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "spreadsheet_write_styles",
                {
                    "file_id": SID,
                    "sheet_name": "Sheet1",
                    "start_row": 0,
                    "start_col": 0,
                    "styles": [[{"background": "ff0000"}]],
                    "confirm": True,
                },
            )
    assert fake_ss.calls == []


@pytest.mark.parametrize(
    ("action", "key", "dimension"),
    [
        ("insert_rows", "insertDimension", "ROWS"),
        ("insert_columns", "insertDimension", "COLUMNS"),
        ("delete_rows", "deleteDimension", "ROWS"),
        ("delete_columns", "deleteDimension", "COLUMNS"),
    ],
)
async def test_batch_update(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi, action: str, key: str, dimension: str
) -> None:
    fake_ss.on("POST", f"{BASE}/batchUpdate", ok())

    await call(
        mcp,
        "spreadsheet_batch_update",
        {
            "file_id": SID,
            "sheet_id": "sh_1",
            "action": action,
            "index": 3,
            "count": 2,
            "confirm": True,
        },
    )

    request = json_body(fake_ss.call())["requests"][0]
    assert list(request) == [key]
    assert request[key]["range"] == {
        "sheetId": "sh_1",
        "dimension": dimension,
        "startIndex": 3,
        "endIndex": 4,
    }


async def test_delete_sheet(mcp: FastMCP, fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("POST", f"{BASE}/sheet/delete", ok({"spreadsheetId": SID}))

    result = await call(
        mcp, "spreadsheet_delete_sheet", {"file_id": SID, "sheet_id": "sh_2", "confirm": True}
    )

    assert json_body(fake_ss.call()) == {"sheetId": "sh_2"}
    assert result == {"success": True, "deleted_sheet_id": "sh_2"}


async def test_delete_file_unregisters(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi, registry: SpreadsheetRegistry
) -> None:
    registry.register("labs", SID, path=None, source="manual")
    fake_ss.on("POST", "/spreadsheets/delete", ok({"spreadsheetId": SID}))

    result = await call(mcp, "spreadsheet_delete_file", {"name": "labs", "confirm": True})

    assert json_body(fake_ss.call()) == {"spreadsheetId": SID}
    assert result == {"success": True, "spreadsheet_id": SID, "unregistered": True}
    assert registry.list() == []


async def test_failed_delete_keeps_registry(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi, registry: SpreadsheetRegistry
) -> None:
    registry.register("labs", SID, path=None, source="manual")
    fake_ss.on("POST", "/spreadsheets/delete", httpx.Response(403))

    message = await call_error(mcp, "spreadsheet_delete_file", {"name": "labs", "confirm": True})

    assert "permission denied" in message
    assert registry.resolve("labs") == SID


async def test_registry_file_lives_in_data_dir(
    mcp: FastMCP, fake_ss: FakeSpreadsheetApi, settings: Settings
) -> None:
    fake_ss.on("POST", "/spreadsheets/create", ok({"spreadsheetId": SID}))

    await call(mcp, "spreadsheet_create", {"name": "Budget"})

    assert (Path(settings.data_dir) / "spreadsheet-ids.json").exists()
