from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from syno_workspace_mcp.clients.spreadsheet import (
    MAX_EXPORT_BYTES,
    SpreadsheetClient,
    block_range,
    column_index,
    column_name,
    parse_cell,
    qualified_range,
)
from syno_workspace_mcp.errors import InvalidInputError, SpreadsheetAuthError, SpreadsheetError
from syno_workspace_mcp.logging import RedactionFilter

from .conftest import (
    SS_TOKEN,
    SS_URL,
    TEST_PASSWORD,
    FakeDsm,
    FakeSpreadsheetApi,
    json_body,
    load_fixture,
)

SID = "AbCdEf1234567890GhIjKl"


@pytest.fixture
async def client(fake_ss: FakeSpreadsheetApi) -> AsyncIterator[SpreadsheetClient]:
    async with SpreadsheetClient(
        SS_URL, "https://nas.example.test:5001", "mcp_service", SecretStr(TEST_PASSWORD)
    ) as ss:
        yield ss


# A1 notation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index", "name"), [(0, "A"), (25, "Z"), (26, "AA"), (701, "ZZ"), (702, "AAA"), (16383, "XFD")]
)
def test_column_names_round_trip(index: int, name: str) -> None:
    assert column_name(index) == name
    assert column_index(name) == index


@pytest.mark.parametrize("cell", ["", "A0", "1A", "A1:B2", "XFE1", "A-1", "ABCD1"])
def test_parse_cell_rejects_invalid(cell: str) -> None:
    with pytest.raises(InvalidInputError):
        parse_cell(cell)


def test_block_range() -> None:
    assert block_range("b3", 2, 3) == "B3:D4"
    assert block_range("A1", 1, 1) == "A1"
    assert block_range("Z1", 1, 2) == "Z1:AA1"


def test_qualified_range_rejects_sheet_in_range() -> None:
    with pytest.raises(InvalidInputError, match="sheet_name"):
        qualified_range("Sheet1", "Other!A1:B2")


# Authorization ----------------------------------------------------------------


async def test_authorizes_once_and_reuses_token(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on(
        "GET", f"/spreadsheets/{SID}", httpx.Response(200, json=load_fixture("ss_get_info.json"))
    )

    await client.get_info(SID)
    await client.get_info(SID)

    assert fake_ss.authorizations == [
        {
            "username": "mcp_service",
            "password": TEST_PASSWORD,
            "host": "nas.example.test:5001",
            "protocol": "https",
        }
    ]
    assert all(r.headers["Authorization"] == f"Bearer {SS_TOKEN}" for r in fake_ss.calls)


async def test_default_port_is_left_out_of_host(fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("GET", f"/spreadsheets/{SID}", httpx.Response(200, json={"id": SID}))
    async with SpreadsheetClient(
        SS_URL, "http://nas.example.test", "mcp_service", SecretStr(TEST_PASSWORD)
    ) as ss:
        await ss.get_info(SID)

    assert fake_ss.authorizations[0]["host"] == "nas.example.test"
    assert fake_ss.authorizations[0]["protocol"] == "http"


async def test_expired_token_is_renewed_and_request_replayed_once(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.authorize_responses = [
        httpx.Response(200, json={"token": "old.token.value"}),
        httpx.Response(200, json={"token": SS_TOKEN}),
    ]
    fake_ss.on(
        "GET",
        f"/spreadsheets/{SID}",
        httpx.Response(401),
        httpx.Response(200, json=load_fixture("ss_get_info.json")),
    )

    info = await client.get_info(SID)

    assert info.name == "fichier_labs"
    assert len(fake_ss.authorizations) == 2
    assert [r.headers["Authorization"] for r in fake_ss.calls] == [
        "Bearer old.token.value",
        f"Bearer {SS_TOKEN}",
    ]


async def test_second_401_is_not_retried_again(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("GET", f"/spreadsheets/{SID}", httpx.Response(401))

    with pytest.raises(SpreadsheetError) as caught:
        await client.get_info(SID)

    assert caught.value.status == 401
    assert len(fake_ss.calls) == 2
    assert len(fake_ss.authorizations) == 2


async def test_authorize_401_explains_the_dsm_back_call(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.authorize_responses = [httpx.Response(401)]

    with pytest.raises(SpreadsheetAuthError) as caught:
        await client.get_info(SID)

    message = str(caught.value)
    assert "https://nas.example.test:5001" in message
    assert "SYNO_SS_DSM_URL" in message
    assert "Synology Office" in message
    assert TEST_PASSWORD not in message
    assert fake_ss.calls == []


async def test_authorize_without_token_fails(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.authorize_responses = [httpx.Response(200, json={})]

    with pytest.raises(SpreadsheetAuthError, match="no token"):
        await client.get_info(SID)


async def test_unreachable_container_names_it(fake_dsm: FakeDsm) -> None:
    fake_dsm.router.route(url__startswith="http://127.0.0.1:3999").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    async with SpreadsheetClient(
        "http://127.0.0.1:3999", "https://nas.example.test", "u", SecretStr("p")
    ) as ss:
        with pytest.raises(SpreadsheetError, match="synology-spreadsheet-api"):
            await ss.get_info(SID)


async def test_close_revokes_the_token(fake_ss: FakeSpreadsheetApi) -> None:
    fake_ss.on("GET", f"/spreadsheets/{SID}", httpx.Response(200, json={"id": SID}))
    async with SpreadsheetClient(SS_URL, "https://nas.example.test", "u", SecretStr("p")) as ss:
        await ss.get_info(SID)

    assert len(fake_ss.revocations) == 1
    assert fake_ss.revocations[0].headers["Authorization"] == f"Bearer {SS_TOKEN}"


async def test_close_without_token_does_not_revoke(fake_ss: FakeSpreadsheetApi) -> None:
    async with SpreadsheetClient(SS_URL, "https://nas.example.test", "u", SecretStr("p")):
        pass

    assert fake_ss.revocations == []


async def test_token_is_redacted_from_logs(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("GET", f"/spreadsheets/{SID}", httpx.Response(200, json={"id": SID}))
    await client.get_info(SID)

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "got %s", (SS_TOKEN,), None)
    RedactionFilter().filter(record)

    assert SS_TOKEN not in record.getMessage()


# Requests -------------------------------------------------------------------


async def test_range_is_percent_encoded_in_one_segment(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    raw = f"/spreadsheets/{SID}/values/My%20Sheet%21A1%3AC3"
    fake_ss.on("GET", raw, httpx.Response(200, json=load_fixture("ss_values.json")))

    cells = await client.get_values(SID, "My Sheet", "A1:C3")

    assert cells.values[0] == ["Name", "Age", "City"]
    assert cells.values[1] == ["Alice", 30, "Paris"]


async def test_write_values_sends_the_block(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("PUT", f"/spreadsheets/{SID}/values/Sheet1%21B2%3AD3", httpx.Response(200, json={}))

    written = await client.write_values(SID, "Sheet1", "B2", [["a", 1, True], ["b"]])

    assert written == "B2:D3"
    assert json_body(fake_ss.call()) == {"values": [["a", 1, True], ["b"]]}


async def test_write_values_rejects_empty_grid(client: SpreadsheetClient) -> None:
    with pytest.raises(InvalidInputError):
        await client.write_values(SID, "Sheet1", "A1", [[]])


async def test_api_error_carries_status_and_detail(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("GET", f"/spreadsheets/{SID}", httpx.Response(404, json={"error": "not found"}))

    with pytest.raises(SpreadsheetError) as caught:
        await client.get_info(SID)

    assert caught.value.status == 404
    assert "not found" in str(caught.value)


async def test_unexpected_shape_is_reported(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on("POST", "/spreadsheets/create", httpx.Response(200, json={"id": "x"}))

    with pytest.raises(SpreadsheetError, match="unexpected"):
        await client.create("New")


async def test_csv_export_sends_sheet_id(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on(
        "GET",
        f"/spreadsheets/{SID}/sheet/csv",
        httpx.Response(
            200,
            content=b"a,b\n1,2\n",
            headers={
                "content-type": "text/csv",
                "content-disposition": "attachment; filename*=UTF-8''fichier%20labs.csv",
            },
        ),
    )

    exported = await client.export(SID, "csv", sheet_id="sh_2")

    assert fake_ss.call().url.params["sheetId"] == "sh_2"
    assert exported.file_name == "fichier labs.csv"
    assert exported.content == b"a,b\n1,2\n"


async def test_export_refuses_oversized_payload(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi
) -> None:
    fake_ss.on(
        "GET",
        f"/spreadsheets/{SID}/xlsx",
        httpx.Response(200, content=b"x" * (MAX_EXPORT_BYTES + 1)),
    )

    with pytest.raises(SpreadsheetError, match="limit"):
        await client.export(SID, "xlsx")


@pytest.mark.parametrize("sheet_id", ["1", "Sheet1", "sh_x", ""])
async def test_invalid_sheet_id_is_rejected_locally(
    client: SpreadsheetClient, fake_ss: FakeSpreadsheetApi, sheet_id: str
) -> None:
    with pytest.raises(InvalidInputError, match="sh_"):
        await client.delete_sheet(SID, sheet_id)

    assert fake_ss.calls == []
